import Reveal from "@/app/components/Reveal";
import SiteHeader from "@/app/components/SiteHeader";

export const metadata = {
  title: "隐私政策 | 选车助手",
};

/** 隐私政策（只收集账号标识与登录凭据，声明收集范围与用途）。 */
export default function PrivacyPage() {
  return (
    <div>
      <SiteHeader />
      <main className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <Reveal>
          <h1 className="text-[30px] font-semibold tracking-tight text-ink sm:text-[34px]">隐私政策</h1>
          <p className="mt-2 text-xs text-ash">生效日期：以页面实际发布时间为准。</p>
        </Reveal>

        <Reveal className="mt-10" delay={60}>
          <div className="glass rounded-[26px] border border-black/[0.05] p-6 sm:p-8">
            <h2 className="text-[17px] font-semibold tracking-tight text-ink">1. 我们收集什么</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-7 text-ink-soft">
              <li>浏览、查看车型详情、SKU 对比、分享链接等功能无需注册，也不收集任何个人信息。</li>
              <li>
                只有在你主动使用「收藏」功能时，才需要登录；登录只收集<strong className="font-semibold text-ink">邮箱地址</strong>
                （或未来接入的手机号），不收集姓名、性别、生日、地址等任何其他信息。
              </li>
              <li>购车助手的对话内容仅保存在你的会话中（服务端短期缓存，过期自动清除），不与你个人身份关联，不用于其他用途。</li>
            </ul>
          </div>
        </Reveal>

        <Reveal className="mt-4" delay={100}>
          <div className="glass rounded-[26px] border border-black/[0.05] p-6 sm:p-8">
            <h2 className="text-[17px] font-semibold tracking-tight text-ink">2. 我们如何使用信息</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-7 text-ink-soft">
              <li>邮箱仅用于登录验证（发送验证码）与账号标识；</li>
              <li>收藏列表仅用于向你自己展示收藏的车型与 SKU；</li>
              <li>我们不会将上述信息出售、出租或用于任何广告营销。</li>
            </ul>
          </div>
        </Reveal>

        <Reveal className="mt-4" delay={140}>
          <div className="glass rounded-[26px] border border-black/[0.05] p-6 sm:p-8">
            <h2 className="text-[17px] font-semibold tracking-tight text-ink">3. 信息保存与删除</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-7 text-ink-soft">
              <li>登录状态与会话数据设有过期时间，过期自动清除；</li>
              <li>
                你可以随时取消收藏；登录状态下可在「我的收藏」页面底部注销账号（不可恢复），注销后账号不再可登录。
              </li>
            </ul>
          </div>
        </Reveal>

        <Reveal className="mt-4" delay={180}>
          <div className="glass rounded-[26px] border border-black/[0.05] p-6 sm:p-8">
            <h2 className="text-[17px] font-semibold tracking-tight text-ink">4. 数据来源与内容说明</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-7 text-ink-soft">
              <li>
                本网站展示的价格均为<strong className="font-semibold text-ink">官方指导价</strong>
                ，配置与销量数据均标注来源与更新时间；缺失数据显示「官方资料未披露」，不做猜测补全。
              </li>
              <li>购车助手的推荐与解释由人工智能生成，仅供参考，不构成购买建议；请以品牌官网信息为准。</li>
              <li>本站不提供交易、询价、优惠与库存信息，相关服务请前往品牌官网。</li>
            </ul>
          </div>
        </Reveal>

        <Reveal className="mt-4" delay={220}>
          <div className="glass rounded-[26px] border border-black/[0.05] p-6 sm:p-8">
            <h2 className="text-[17px] font-semibold tracking-tight text-ink">5. 联系我们</h2>
            <p className="mt-3 text-sm leading-7 text-ink-soft">
              如对本政策有任何疑问，可通过站点上线时公布的客服邮箱与我们联系。
            </p>
          </div>
        </Reveal>
      </main>
    </div>
  );
}
