import Link from "next/link";

export default function Home() {
  return (
    <main className="shell home">
      <header className="topbar">
        <Link className="brand" href="/">
          <span className="brand-mark" aria-hidden="true" />
          travel / workspace
        </Link>
        <nav className="nav"><Link href="/trips">我的旅行</Link><Link href="/trips/new">新建旅行</Link></nav>
      </header>
      <section className="home-hero">
        <div>
          <div className="eyebrow">AI TRAVEL INFORMATION ENGINE</div>
          <h1>Facts first.<br />Plans that adapt.</h1>
          <p className="home-lede">把分散的酒店、餐厅、景点、路线和预算放进同一个工作区。先查事实，再做决策；每次修改都留下来源、版本与影响。</p>
          <div style={{ display: "flex", gap: 10, marginTop: 28, flexWrap: "wrap" }}>
            <Link className="button" href="/trips/new">开始一次规划 →</Link>
            <Link className="button secondary" href="/trips">打开我的旅行</Link>
          </div>
        </div>
        <aside className="home-panel">
          <div className="home-index">01 / WORKSPACE</div>
          <div>
            <div className="eyebrow" style={{ color: "#91adff" }}>CURRENT MODE</div>
            <h2 style={{ fontSize: 32, lineHeight: 1.05, letterSpacing: "-.06em", margin: "15px 0" }}>One trip.<br />One source of truth.</h2>
            <p>AI 负责理解、调用工具和解释。酒店报价、预算、营业时间与路线由结构化引擎计算并可追溯。</p>
          </div>
          <div style={{ color: "#b7c0cc", fontSize: 12 }}>Free sources: OpenStreetMap · Open-Meteo · OSRM</div>
        </aside>
      </section>
      <section className="principles">
        <div className="principle"><div className="eyebrow">01</div><strong>事实</strong><span>价格与营业状态不由模型臆测。</span></div>
        <div className="principle"><div className="eyebrow">02</div><strong>来源</strong><span>每个关键事实保留 Provider 与更新时间。</span></div>
        <div className="principle"><div className="eyebrow">03</div><strong>决策</strong><span>硬约束、候选评分、预算与路线由程序完成。</span></div>
        <div className="principle"><div className="eyebrow">04</div><strong>版本</strong><span>自然语言修改只影响必要部分，并写入历史。</span></div>
      </section>
    </main>
  );
}
