# 共享目录 `.tmp` 权限事故：容器写不进去时，**没有任何报错**（2026-10-23）

> 触发：发现夜间任务每晚都在白重建向量索引，追查下去发现是**权限**问题。
> 结论：宿主 `backend/.tmp` 权限位被改坏 ⇒ 容器内身份写不进去 ⇒ **静默退化**。
> 分支：`fix/deploy-shared-dir-perm`

## 一、症状：两个都**不报错**

- 夜间任务每晚都「全量重建」向量索引（5 分钟 CPU + embedding 费用），但**销量数据根本没变**；
- 管理审计日志不落盘（只剩 docker logs 里那一份）——审计链断了一半。

两个症状指向同一个根因：**`backend/.tmp` 写不进去**。

写不进去为什么没人发现？因为审计写入把 `OSError` **吞了**（`app/common/admin_auth.py`），
水位标记写失败也只是「状态页判落后」。⇒ 失败被降级成了静默的退化。

## 二、根因：权限位是**唯一**依据

| 事实 | 出处 |
| --- | --- |
| 宿主 `backend/.tmp` 被 bind-mount 进 api 容器 | `deploy/docker-compose.yml` |
| compose 里 `cap_drop: ALL` | 同上 |
| ⇒ 连**以 root 跑**的容器都没有 `DAC_OVERRIDE` | 能力被丢掉了 |

⇒ **uid 0 ≠ 无所不能**：在这个 compose 下，root 容器的写权限**完全由权限位决定**。
当时宿主上 `backend/.tmp` 是 `root:root 755` ⇒ 容器内身份写不进去。

## 三、为什么需要**两种**身份都能写

| 身份 | 在哪写 `.tmp` | 写什么 |
| --- | --- | --- |
| 容器内 uid 10001（`carsel`） | api 进程内 | 审计日志、dense 水位标记 |
| 宿主 root | `deploy/carsel-nightly.sh` | 读/删 `sales-changed.flag`、读水位标记 |

⇒ 权限位必须同时覆盖这两个身份。

## 四、修法（`deploy/carsel-deploy.sh` 第 5 步「切流」）

```
mkdir -p "$APP_DIR/backend/.tmp"
chmod 1777 "$APP_DIR/backend/.tmp" 2>/dev/null || true
find "$APP_DIR/backend/.tmp" -maxdepth 1 -type f -exec chmod 666 {} + 2>/dev/null || true
chown -R 10001:10001 "$APP_DIR/backend/.tmp" 2>/dev/null || true
```

四处都有理由，不是随手写的：

1. **目录 1777 而不是 777**：sticky 位保证「谁都能写，但只能删自己的文件」，
   避免一个身份误删另一个身份的文件。
2. **现有文件逐个 chmod 666**：新建文件的权限由 umask 决定 ⇒
   光 chmod 目录对**已存在**的文件无效，老文件仍然写不进去。
3. **chown 在 chmod 之后**：chown 可能清掉 setuid/setgid 位，顺序反了刚放开的权限位会被抹掉。
4. **`|| true` 兜住**：写不进去的代价是「审计与水位标记退化」，不是「站点起不来」
   ⇒ 授权失败**不得**中断部署。

## 五、已做与待做

- ✅ **生产已即时处置**：`chmod 1777` 目录 + `chmod 666` 现有文件；
  实测 **root 容器 / uid 10001 / 宿主 root** 三种身份均可写；审计日志已重新落盘
  （`admin-audit.log` 尾部可见 `POST /api/v1/admin/rag/query status=200`）。
- ✅ 夜间任务 2026-10-20 → 2026-10-23 连续四天「无新销量月份，跳过重建」⇒ 现象消失。
- ⏳ **本笔把修法固化进部署脚本**：否则一次 `rsync`/手工改动就能把权限改回原样，
  而**改回去不会有任何报错**。
- ⏳ 契约测试 `backend/tests/test_deploy_shared_dir_perm.py`（8 条）钉住判据，
  6 个变异点全部被验证能被抓到（`chmod 777` / 去掉文件放开 / 改成 0:0 / 调换顺序 /
  去掉容错 / 挪到容器启动之后）。

## 六、诚实的边界

- 本次只证明**当前**权限位是对的。**没有**做「权限被改坏时自动发现并告警」——
  下次若有人再把权限改坏，仍然要等夜间任务白重建一轮才看得出来。
  真正的修法是**让写入失败可见**（审计写入别吞 `OSError`），属另一笔改动，未做。
- 生产容器**目前仍以 root 运行**（非 root 那版尚未部署）⇒ 本次修的是
  「权限位是唯一依据」这个前提下的后果；等容器真正以 uid 10001 跑起来后，
  本次授权依然成立（属主就是 10001）。
