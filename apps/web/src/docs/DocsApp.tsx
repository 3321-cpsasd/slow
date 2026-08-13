import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import './docs.css';

type DocPage = {
  slug: string;
  group: '开始使用' | '核心概念' | '学习方法' | '信任与支持';
  title: string;
  description: string;
  keywords: string;
  readingTime: string;
  content: ReactNode;
};

const STANDALONE_DOCS = import.meta.env.VITE_DOCS_ONLY === 'true';
const PUBLIC_BASE = import.meta.env.BASE_URL.replace(/\/$/, '');
const DOCS_BASE_PATH = STANDALONE_DOCS ? PUBLIC_BASE || '/' : `${PUBLIC_BASE}/docs`;

function docsPath(slug: string) {
  if (slug === 'welcome') return STANDALONE_DOCS
    ? `${DOCS_BASE_PATH === '/' ? '' : DOCS_BASE_PATH}/`
    : DOCS_BASE_PATH || '/';
  return `${DOCS_BASE_PATH === '/' ? '' : DOCS_BASE_PATH}/${slug}`;
}

function Callout({ kind = 'note', title, children }: {
  kind?: 'note' | 'good' | 'warn';
  title: string;
  children: ReactNode;
}) {
  return (
    <aside className={`doc-callout ${kind}`}>
      <span aria-hidden="true">{kind === 'good' ? '✓' : kind === 'warn' ? '!' : 'i'}</span>
      <div><b>{title}</b><p>{children}</p></div>
    </aside>
  );
}

function StepList({ children }: { children: ReactNode }) {
  return <ol className="doc-steps">{children}</ol>;
}

function FigureHierarchy() {
  return (
    <figure className="hierarchy-figure" aria-labelledby="hierarchy-caption">
      <div className="hierarchy-track">
        {[
          ['书架', '一个长期领域'],
          ['系列', '一个学习目标'],
          ['书', '一个完整主题'],
          ['章', '一天左右的阶段'],
          ['节', '一个核心知识点'],
        ].map(([name, note], index) => (
          <div className="hierarchy-node" key={name}>
            <span>{index + 1}</span><b>{name}</b><small>{note}</small>
          </div>
        ))}
      </div>
      <figcaption id="hierarchy-caption">从长期领域逐层收束到一次可以完成、可以验证的学习。</figcaption>
    </figure>
  );
}

function FigureLoop() {
  return (
    <figure className="loop-figure" aria-labelledby="loop-caption">
      <div className="loop-line" aria-hidden="true" />
      {[
        ['读', '理解一个核心知识点'],
        ['问', '在具体段落上追问'],
        ['验', '完成本节选择题'],
        ['进', '及格后解锁下一节'],
        ['记', '证据写回学习画像'],
      ].map(([name, note], index) => (
        <div className="loop-stop" key={name}>
          <span>{index + 1}</span><b>{name}</b><small>{note}</small>
        </div>
      ))}
      <figcaption id="loop-caption">Slow 的学习闭环不是“生成后收藏”，而是每一节都完成阅读、验证和积累。</figcaption>
    </figure>
  );
}

const pages: DocPage[] = [
  {
    slug: 'welcome',
    group: '开始使用',
    title: '欢迎来到 Slow',
    description: '了解 Slow 如何把一个学习目标变成能逐节学完的个性化教材。',
    keywords: 'Slow 是什么 AI 学习 教材 个性化 新手 开始',
    readingTime: '3 分钟',
    content: (
      <>
        <p className="doc-lead">Slow 是一个 AI 原生个人学习系统。你给出真正想完成的学习目标，它会把目标组织成书，并陪你逐节阅读、验证和积累，而不是停在一份看起来很完整的学习计划上。</p>
        <h2 id="where-to-start">从哪里开始</h2>
        <div className="doc-card-grid docs-path-grid">
          <a href={docsPath('learning-structure')}><span>学习者</span><b>先认识学习书架</b><p>了解书架、系列、书、章与节，再创建第一个真实目标。</p><i aria-hidden="true">→</i></a>
          <a href={docsPath('textbook-not-plan')}><span>理解项目</span><b>从核心概念开始</b><p>理解教材闭环、Learning Contract、学习证据与内容发布边界。</p><i aria-hidden="true">→</i></a>
          <a href="https://github.com/3321-cpsasd/slow"><span>参与共建</span><b>进入 GitHub 仓库</b><p>查看源码、架构决策、开发方式与当前项目状态。</p><i aria-hidden="true">↗</i></a>
        </div>
        <FigureLoop />
        <h2 id="what-you-get">你最终得到什么</h2>
        <p>每个学习目标会形成一个系列。系列里有按顺序学习的书，每本书围绕一个完整主题展开。你在小节中学习一个核心知识点，通过验证后继续下一节。</p>
        <div className="doc-card-grid">
          <article><span>01</span><b>为你生成的书</b><p>内容会参考你的背景、目标、已有经验和后续学习证据。</p></article>
          <article><span>02</span><b>真实的学习进度</b><p>进度来自完成的小节和验证结果，不来自打开页面或收藏计划。</p></article>
          <article><span>03</span><b>持续更新的画像</b><p>测验与口试证据会帮助后续教材减少重复、补足薄弱连接。</p></article>
        </div>
        <h2 id="first-session">第一次使用</h2>
        <StepList>
          <li><b>完成学习画像</b><p>告诉 Slow 你的背景、目标、已有经验和每周可投入时间。</p></li>
          <li><b>创建一个书架</b><p>用长期领域命名，例如“计算机科学”“投资研究”或“设计史”。</p></li>
          <li><b>说清学习目标</b><p>描述希望获得的能力、使用场景和时间要求，而不只是输入一个宽泛主题。</p></li>
          <li><b>确认第一本书的目录</b><p>检查方向是否符合你的目标；确认后，Slow 才开始准备正式学习内容。</p></li>
        </StepList>
        <Callout kind="good" title="从一个真实目标开始">好的目标通常包含“学什么、为了什么、最终要做到什么”。例如：“六周内掌握 Python 数据分析，能够独立清洗并分析工作中的 CSV 报表。”</Callout>
      </>
    ),
  },
  {
    slug: 'learning-structure',
    group: '开始使用',
    title: '认识你的学习书架',
    description: '分清书架、系列、书、章与节，知道进度记录在哪里。',
    keywords: '书架 系列 学习目标 书 章 节 层级 目录 编号 进度',
    readingTime: '4 分钟',
    content: (
      <>
        <p className="doc-lead">Slow 用人们熟悉的“书”来组织学习，但每一层都有明确职责。理解这套结构，能让你更准确地创建目标、判断目录，也能知道系统正在记录什么。</p>
        <FigureHierarchy />
        <h2 id="shelf">书架：长期领域</h2>
        <p>书架是一个学科、领域或专业的容器。它可以容纳多个目标与多本书，也承载你在这个领域逐渐形成的学习记忆。</p>
        <p><b>适合：</b>“计算机科学”“心理学”“产品设计”。<br /><b>不适合：</b>“这周学完递归”——这更像一个目标或章节范围。</p>
        <h2 id="series">系列：一个已确认的学习目标</h2>
        <p>每个学习目标形成一个系列。系列把完成目标所需的书按顺序组织起来，也是目标级进度的载体。</p>
        <h2 id="book-chapter-section">书、章与节</h2>
        <div className="definition-list">
          <div><b>书</b><p>围绕一个完整、可命名的主题展开，通常需要数天完成。它不是一个章节的包装。</p></div>
          <div><b>章</b><p>一组相关知识点构成的学习阶段，通常对应约一天，不为凑数量机械拆分。</p></div>
          <div><b>节</b><p>一次以一个核心知识点为锚点的学习，典型投入约 15–20 分钟，结尾必须完成验证。</p></div>
        </div>
        <Callout title="编号怎样阅读">“第 2 本”是系列里的第二本书；“第 3 章”是这本书的第三章；“3.2”表示第三章的第二节。书序号不会和章序号拼成小数。</Callout>
        <h2 id="content-blocks">正文里的标题不是新目录</h2>
        <p>定义、机制、例子、边界、练习和小结只是小节内部的教学步骤。它们帮助讲清当前知识点，不会各自形成进度或解锁层级。</p>
      </>
    ),
  },
  {
    slug: 'create-a-goal',
    group: '开始使用',
    title: '创建一个好目标',
    description: '把模糊的“我想学”改写成可以规划、学习和验证的目标。',
    keywords: '创建 目标 规划 书架 系列 目录 背景 时间 能力',
    readingTime: '5 分钟',
    content: (
      <>
        <p className="doc-lead">目标越具体，教材越能做出真正有用的取舍。Slow 不需要你预先设计课程，但需要知道你的起点、用途和终点。</p>
        <h2 id="four-parts">一个好目标的四个部分</h2>
        <div className="goal-formula" aria-label="好目标的组成">
          <span>当前基础</span><i>＋</i><span>目标能力</span><i>＋</i><span>使用场景</span><i>＋</i><span>时间边界</span>
        </div>
        <div className="before-after">
          <div><small>太宽泛</small><p>我想学习机器学习。</p></div>
          <span aria-hidden="true">→</span>
          <div><small>更适合规划</small><p>我会 Python 和基础统计，希望八周内理解常见监督学习方法，并能为业务表格数据训练、评估和解释一个基线模型。</p></div>
        </div>
        <h2 id="confirmation">为什么要确认目录</h2>
        <p>目标确认后，Slow 会先规划完整书单和各书的初始章节方向。第一本书的目录需要你确认，确认意味着主题、顺序和范围符合你的真实意图。</p>
        <Callout kind="warn" title="目录确认不是同意所有未来正文">正文仍会逐节生成并经过结构与范围检查。后续书籍会在前一本完成后，参考你最新的学习证据重新校准，再请你确认。</Callout>
        <h2 id="adjust">什么时候应该调整</h2>
        <ul>
          <li>书名与目标没有清晰关系；</li>
          <li>第一本书明显重复你已经稳定掌握的内容；</li>
          <li>缺少完成目标不可绕过的基础；</li>
          <li>计划强度与你可投入的时间明显不符。</li>
        </ul>
        <p>如果只是对某个标题的措辞不熟悉，可以先看它的章节说明；不要为了得到“完美目录”无限推迟开始学习。</p>
      </>
    ),
  },
  {
    slug: 'textbook-not-plan',
    group: '核心概念',
    title: '教材，不是学习计划',
    description: '理解 Slow 的核心交付物，以及它为什么必须包含正文、验证和连续进度。',
    keywords: '核心概念 教材 学习计划 AI 原生 闭环 正文 验证 进度',
    readingTime: '4 分钟',
    content: (
      <>
        <p className="doc-lead">许多 AI 学习产品的终点是生成一张路线图。Slow 把路线图当作起点：真正的交付物是学习者能够逐节阅读、验证，并在后续内容中继续使用学习证据的个性化教材。</p>
        <h2 id="difference">两者的关键差别</h2>
        <div className="before-after">
          <div><small>学习计划</small><p>说明将来应该学什么。生成完成后，执行和验证通常留给学习者自己。</p></div>
          <span aria-hidden="true">→</span>
          <div><small>Slow 教材</small><p>直接提供可学习的正文、与正文对应的验证、渐进解锁，以及会影响后续教材的学习记录。</p></div>
        </div>
        <h2 id="minimum-unit">最小闭环是“一节”</h2>
        <p>每节以一个核心知识点和一个主要问题为锚点。它可以调用必要的前置、机制、对比、边界与应用知识，但必须在一次合理投入中形成完整理解，并以正式验证结束。</p>
        <FigureLoop />
        <h2 id="why-books">为什么仍然使用书、章、节</h2>
        <p>AI 可以即时生成内容，但学习仍需要范围、顺序和完成感。书提供完整主题，章形成阶段性聚合，节把行动压缩为今天可以完成的一步。这套层级同时约束课程规划、导航、进度和学习证据，不能被临时生成结果随意改写。</p>
        <Callout kind="good" title="判断一个功能是否属于 Slow">它是否让用户更接近“真正学完并留下可用证据”？如果只让计划看起来更丰富，却没有改善阅读、验证或连续学习，它就不是核心交付。</Callout>
      </>
    ),
  },
  {
    slug: 'learning-contract',
    group: '核心概念',
    title: 'Learning Contract',
    description: '理解一节课究竟允许教授和考核什么，以及为什么这条边界必须先于正文冻结。',
    keywords: 'Learning Contract 学习契约 考核目标 脚手架 稳定 ID 前置缺口',
    readingTime: '5 分钟',
    content: (
      <>
        <p className="doc-lead">Learning Contract 是 Slow 内部用于约束一节课考核范围的版本化约定。它在正文生成前确定：这一节必须让学习者掌握什么、用什么问题验证，以及哪些目标有资格形成掌握证据。</p>
        <Callout title="这是项目概念，不是学习界面术语">普通用户只需要看到“本节学什么、现在是什么状态、下一步做什么”。Learning Contract、稳定 ID 和发布检查保留在项目文档与开发者审计中。</Callout>
        <h2 id="three-jobs">它解决三个问题</h2>
        <div className="doc-card-grid">
          <article><span>01</span><b>锁定考核范围</b><p>测验只能检查契约中已经明确的目标，不能因为正文顺带提到某个概念就临时加考。</p></article>
          <article><span>02</span><b>连接正文与证据</b><p>每个必需目标都必须既在正文中得到教授，也在题目中得到测量。</p></article>
          <article><span>03</span><b>阻止证据污染</b><p>支撑性知识可以帮助理解，但不会静默变成新的掌握结论。</p></article>
        </div>
        <h2 id="scaffolding">目标与脚手架不是一回事</h2>
        <p>为了讲清当前知识点，正文可能补充一个薄弱前置概念、一个反例或一段背景。这些内容承担脚手架作用。除非它们已经被正式加入当前 Learning Contract，否则不能进入测验，也不能形成“用户已经掌握”的证据。</p>
        <h2 id="gap">大型前置缺口怎么办</h2>
        <p>如果缺口太大，无法在当前小节中补足而又不引入新的并列目标，正文生成必须退出，由课程规划显式增加或调整前置小节。生成器没有权力为了完成任务把多个核心知识点塞进一节。</p>
        <h2 id="stable">为什么目标需要稳定身份</h2>
        <p>自然语言标题可以修改或出现近义表达，而正文、题目和学习证据必须持续指向同一个目标。稳定目标 ID 让版本变化可追溯，也避免系统凭文本相似度猜测并错误合并概念。</p>
      </>
    ),
  },
  {
    slug: 'evidence-and-personalization',
    group: '核心概念',
    title: '学习证据与个性化',
    description: '了解哪些行为会形成掌握证据，以及这些证据如何改变后续教材。',
    keywords: '学习证据 个性化 掌握画像 测验 Ask Me Ask AI 投影 跨书',
    readingTime: '5 分钟',
    content: (
      <>
        <p className="doc-lead">个性化不是把用户姓名写进例子，也不是每次都重新猜测偏好。Slow 的个性化来自两类输入：用户主动提供的学习画像，以及正式学习过程中留下的可追溯证据。</p>
        <h2 id="facts">什么会形成正式证据</h2>
        <div className="definition-list">
          <div><b>测验</b><p>记录一节约定目标上的作答与结果，是解锁和掌握判断的基础证据。</p></div>
          <div><b>Ask Me</b><p>探测机制、边界和迁移能力，为已经满分的知识点补充更深入的证据。</p></div>
          <div><b>Ask AI</b><p>帮助用户继续理解，但问答本身不是考核，不会自动产生掌握结论。</p></div>
        </div>
        <h2 id="raw-and-projection">事实与投影分开</h2>
        <p>一次作答、一次口试和它们所绑定的内容版本是原始事实；“当前掌握情况”“待复习概念”和进度统计是根据规则计算出的投影。投影可以随着规则更新重新构建，不能反过来覆盖原始事实。</p>
        <h2 id="feedback-loop">证据如何进入下一本书</h2>
        <StepList>
          <li><b>完成正式验证</b><p>结果与当时使用的正文、题目和目标版本一起记录。</p></li>
          <li><b>更新概念掌握画像</b><p>系统把相关证据汇总为当前可用的掌握判断。</p></li>
          <li><b>筛选与新内容相关的证据</b><p>生成上下文只使用当前主题需要的部分，不发送全部历史记录。</p></li>
          <li><b>重新校准后续教材</b><p>高掌握概念减少重复，薄弱关联获得更多脚手架；下一本书的目录会再次请用户确认。</p></li>
        </StepList>
        <Callout kind="warn" title="个性化不等于降低标准">表达方式和脚手架可以因人而异，但考核边界、通过门槛和正式学习证据不能为了让结果好看而静默降低。</Callout>
      </>
    ),
  },
  {
    slug: 'content-lifecycle',
    group: '核心概念',
    title: '内容版本与发布边界',
    description: '分清模型候选、生成尝试和正式教材，理解 Slow 为什么坚持失败关闭。',
    keywords: '内容版本 发布边界 GenerationAttempt ContentVersion 候选 原子发布 失败关闭',
    readingTime: '6 分钟',
    content: (
      <>
        <p className="doc-lead">模型返回了一段正文，不代表教材已经发布。Slow 把“模型尝试生成了什么”与“学习者正式学到了什么版本”分成两类权威对象，防止半成品进入阅读、测验和学习证据链。</p>
        <Callout title="两个对象，两种职责"><b>GenerationAttempt</b> 记录一次生成运行及其结果；<b>ContentVersion</b> 才是正式教材版本。数据库里有记录、任务显示完成或 URL 可以访问，都不自动等于内容已发布。</Callout>
        <h2 id="path">从候选到正式教材</h2>
        <StepList>
          <li><b>冻结生成输入</b><p>确定本节目的、Learning Contract、相邻范围和相关学习证据。</p></li>
          <li><b>生成完整候选</b><p>正文、测验和局部对应关系在同一次模型调用中产生。</p></li>
          <li><b>执行确定性校验</b><p>检查结构、目标成员关系、正文与题目的覆盖以及引用是否闭合。</p></li>
          <li><b>原子发布</b><p>正文版本、题集、目标绑定和学习实例版本一起成功提交；任何一步失败都不产生部分正式内容。</p></li>
        </StepList>
        <h2 id="fail-closed">什么是失败关闭</h2>
        <p>契约外目标、缺失的必需目标、悬空引用或无法容纳的前置缺口都会让整份候选失败。系统不能删除坏题后发布剩余内容，也不能猜测最接近的目标或降低门槛来让结果通过。</p>
        <h2 id="quality-boundary">结构校验不等于事实核验</h2>
        <p>这些规则能确定“内容有没有越权考核、证据链是否完整”，不能证明“每个事实都正确、解释一定清晰、题目绝无歧义”。事实与教学质量仍依靠更强模型、离线评测、用户纠错和真实学习表现持续改进。</p>
        <h2 id="versions">正式版本如何变化</h2>
        <p>已发布版本不会被静默覆盖。新版本发布后，旧版本可以进入被替代或撤回状态；历史测验和学习证据仍绑定当时实际使用的版本，因此可以追溯和重新计算。</p>
      </>
    ),
  },
  {
    slug: 'data-trust',
    group: '核心概念',
    title: '权威事实、投影与 Demo',
    description: '理解 Slow 的数据可信原则：谁能写入、如何追溯，以及为什么模拟数据必须明示。',
    keywords: '数据可信 权威 来源 血缘 投影 缓存 Demo Mock 审计 隐私',
    readingTime: '5 分钟',
    content: (
      <>
        <p className="doc-lead">“数据库里有一行”不代表内容已经被证实，“页面显示 80%”也不代表 80% 是不可改变的事实。Slow 为每类重要数据区分权威来源、派生投影和运行审计。</p>
        <h2 id="principles">五条数据可信原则</h2>
        <div className="definition-list concept-principles">
          <div><b>权威唯一</b><p>每类数据有明确的唯一写入者；前端、缓存和统计不能反向覆盖正式事实。</p></div>
          <div><b>来源可溯</b><p>重要内容能追溯到运行模式、上游依据以及生成或规则版本。</p></div>
          <div><b>模拟明示</b><p>Demo、Mock 和测试数据必须可识别、环境隔离，不能伪装成真实内容或真实学习证据。</p></div>
          <div><b>派生可重建</b><p>进度、掌握情况和统计是投影，必须能够从原始事实和确定版本规则重新生成。</p></div>
          <div><b>变更留痕</b><p>重要生成、发布和状态变化需要版本化，不允许静默覆盖、降级或修正。</p></div>
        </div>
        <h2 id="audit-layers">为什么审计也要分层</h2>
        <p>开发者需要完整血缘、模型版本、失败原因和运行诊断；普通用户只需要知道当前状态、影响和下一步。隐藏实现细节只改变表达层，不能绕过发布检查、权限边界、版本留痕或学习证据规则。</p>
        <h2 id="demo-boundary">Demo 模式的边界</h2>
        <p>没有外部模型配置时，开发环境可以显式启用本地 Demo 数据。Demo 内容用于体验和测试，不得进入正式用户的真实进度、掌握画像或运营结论。正式模式也不能在模型不可用时静默生成模拟内容。</p>
        <Callout kind="good" title="阅读项目数据时先问三个问题">它的权威来源是什么？当前看到的是原始事实还是派生结果？这个结果绑定了哪个内容、规则或运行版本？</Callout>
      </>
    ),
  },
  {
    slug: 'study-a-section',
    group: '学习方法',
    title: '完成一节学习',
    description: '从进入正文到完成验证，走完一次 15–20 分钟的学习闭环。',
    keywords: '小节 正文 阅读 快速 完整 模式 笔记 学习',
    readingTime: '5 分钟',
    content: (
      <>
        <p className="doc-lead">一节围绕一个核心知识点展开。正文会调用必要的前置知识、机制、对比、边界与应用，但考核目标不会在阅读中悄悄增加。</p>
        <h2 id="choose-mode">选择今天的阅读节奏</h2>
        <div className="mode-compare">
          <article><span className="mode-dot slow" /><b>完整阅读</b><p>展示本节全部正文与学习工具，适合第一次系统学习。</p></article>
          <article><span className="mode-dot fast" /><b>快速阅读</b><p>聚焦关键段落并提供短自检，适合时间有限时保持连续性。</p></article>
        </div>
        <Callout title="模式不会改变验证标准">无论使用哪种阅读节奏，正式解锁仍以本节的验证结果为准。</Callout>
        <h2 id="read">阅读正文</h2>
        <StepList>
          <li><b>先看本节问题</b><p>带着本节要回答的问题阅读，避免把正文当作需要逐字记忆的资料。</p></li>
          <li><b>在卡住的段落停下来</b><p>使用段落旁的帮助入口，请 Ask AI 换一种解释、补一个例子或回答具体疑问。</p></li>
          <li><b>完成必要练习</b><p>先独立尝试，再对照解释。能复述不等于能在新情境里使用。</p></li>
          <li><b>进入本节验证</b><p>用选择题检查本节约定的学习目标是否已经掌握。</p></li>
        </StepList>
        <h2 id="blocked">如果正文暂时没有准备好</h2>
        <p>Slow 只会把正文、练习和测验完整对应的内容提供给你。如果内容没有准备完整，本节会保持等待状态并提供重试入口；不会用残缺内容继续计算进度。</p>
      </>
    ),
  },
  {
    slug: 'quiz-and-unlock',
    group: '学习方法',
    title: '测验、解锁与补救',
    description: '理解及格、满分、下一节解锁和答错后的补救机制。',
    keywords: '测验 选择题 及格 满分 解锁 补救 重试 掌握度 证据',
    readingTime: '4 分钟',
    content: (
      <>
        <p className="doc-lead">每节都以选择题验证结束。它不是附加练习，而是 Slow 确认你可以继续前进、并形成学习证据的必要环节。</p>
        <h2 id="pass">及格之后</h2>
        <p>达到本节及格线后，下一节解锁，本次结果会进入你的学习记录。解锁状态和掌握情况不能通过手动操作页面来更改。</p>
        <h2 id="perfect">满分之后</h2>
        <p>满分后会出现可选的 Ask Me 隐藏关卡。它通过多轮口试依次探测机制、边界与迁移能力，帮助判断你是否真的能解释和使用所学内容。</p>
        <Callout kind="good" title="Ask Me 是加深证据，不是解锁门槛">你不必完成 Ask Me 才能继续下一节。它适合在重要知识点上主动挑战自己。</Callout>
        <h2 id="retry">没有及格怎么办</h2>
        <p>一次未通过不会抹掉已完成的阅读。系统会根据错误反映出的薄弱点提供补救，再让你重新验证。新的题目应检查同一学习目标，而不是只要求记住上一轮答案。</p>
        <h2 id="evidence">结果如何影响未来内容</h2>
        <p>测验和 Ask Me 产生的证据会写回概念掌握画像。后续生成新书或调整学习路径时，高掌握概念会减少重复讲解，薄弱关联会得到更多脚手架。</p>
        <Callout kind="warn" title="掌握情况不是人格评价">它只反映你在特定概念上已有的学习证据，用来帮助调整后续内容，并会随着继续学习而变化。</Callout>
      </>
    ),
  },
  {
    slug: 'ask-tools',
    group: '学习方法',
    title: 'Ask AI 与 Ask Me',
    description: '在阅读中获得帮助，或在满分后挑战更深入的口试。',
    keywords: 'Ask AI Ask Me 提问 口试 段落 解释 隐藏关卡',
    readingTime: '4 分钟',
    content: (
      <>
        <p className="doc-lead">两个名字相似的工具承担完全不同的任务：Ask AI 帮你在阅读中理解；Ask Me 在学习完成后检查你是否能独立说明和迁移。</p>
        <div className="ask-compare">
          <article><small>阅读中的帮助</small><h2>Ask AI</h2><p>绑定到当前小节和具体段落。你可以追问概念、请求换一种解释或补一个例子，它不会把问答插进正文主流程。</p><b>目标：继续理解</b></article>
          <article><small>满分后的挑战</small><h2>Ask Me</h2><p>按机制、边界与迁移逐步提问。考核过程中不会继续教学，回答会形成更深入的掌握证据。</p><b>目标：证明会用</b></article>
        </div>
        <h2 id="good-question">怎样向 Ask AI 提问</h2>
        <p>先从最具体的卡点开始，并说明你在哪里失去理解。相比“再解释一下”，下面这些问题更有效：</p>
        <ul>
          <li>“从这一步到下一步，为什么可以做这个变换？”</li>
          <li>“给我一个不满足这个边界条件的反例。”</li>
          <li>“用我熟悉的 Python 列表类比这里的数据结构。”</li>
          <li>“只提示第一步，不要直接给完整答案。”</li>
        </ul>
        <h2 id="askme-tip">怎样完成 Ask Me</h2>
        <p>用自己的语言回答，明确说明因果关系和适用边界。遇到迁移题时，先判断新情境与正文例子的相同点和不同点，再给出结论。</p>
      </>
    ),
  },
  {
    slug: 'ai-content',
    group: '信任与支持',
    title: 'AI 内容与参考来源',
    description: '了解哪些内容由 AI 生成、发布前检查什么，以及哪些事情没有被承诺。',
    keywords: 'AI 内容 参考 来源 事实 核验 校验 Demo 透明 可信',
    readingTime: '5 分钟',
    content: (
      <>
        <p className="doc-lead">Slow 会明确区分 AI 内容、Demo 内容与参考来源。系统对内容的结构、学习范围和证据边界执行严格检查，但不会把这些检查描述成对所有事实的独立核验。</p>
        <h2 id="generated">正文如何成为正式内容</h2>
        <StepList>
          <li><b>先确定本节目标</b><p>本节要教授和验证的核心目标在正文生成前确定。</p></li>
          <li><b>一起准备正文与测验</b><p>正文、选择题和对应的正文位置会作为一份完整内容一起准备。</p></li>
          <li><b>完成发布前检查</b><p>Slow 检查结构是否完整、测验是否只覆盖约定目标、题目与正文是否对应、必需目标是否覆盖。</p></li>
          <li><b>完整提供或保持等待</b><p>只有完整通过检查的内容才进入阅读与学习记录；不完整的结果不会作为普通正文展示。</p></li>
        </StepList>
        <Callout kind="warn" title="检查边界">这些检查关注内容是否完整、范围是否一致、题目与正文是否对应，不等于每个事实都经过外部来源的逐条核验。重要决策仍应核对专业的一手资料。</Callout>
        <h2 id="sources">参考来源代表什么</h2>
        <p>当页面展示参考来源时，它们帮助你追溯主题依据和继续阅读。来源存在不意味着正文每一句都由该来源逐字支持，也不意味着系统完成了全面事实审查。</p>
        <h2 id="demo">Demo 内容</h2>
        <p>本地开发或明确的体验模式可能使用 Demo 数据。它会被机器识别并在界面中标明，不能伪装成真实 AI 生成、真实核验或正式学习证据。</p>
        <h2 id="feedback">发现问题怎么办</h2>
        <p>使用正文段落旁的“反馈这段”报告具体内容问题；使用页面边缘的“反馈”报告产品问题或建议。尽量说明哪里不准确、你预期看到什么，不要提交密码、恢复码或其他敏感信息。</p>
      </>
    ),
  },
  {
    slug: 'account-and-privacy',
    group: '信任与支持',
    title: '账号、隐私与数据',
    description: '了解账号恢复、学习数据用途、退出内测和删除申请。',
    keywords: '账号 密码 恢复码 隐私 数据 删除 退出 内测 安全',
    readingTime: '5 分钟',
    content: (
      <>
        <p className="doc-lead">Slow 使用你的学习画像和学习证据来持续调整教材。你应当知道系统为什么需要这些数据、它们如何影响体验，以及如何管理账号和退出。</p>
        <h2 id="recovery">密码与恢复码</h2>
        <p>无邮箱账号不依赖邮箱或手机找回。注册或重置密码后，系统会展示一次恢复码，但不会保存可以直接查看的恢复码原文。请把它放在可靠的密码管理器中。</p>
        <Callout kind="warn" title="恢复码只展示一次">密码和恢复码同时遗失后，无法自助恢复账号。生成新恢复码或完成密码重置后，旧恢复码和已有登录会话都会失效。</Callout>
        <h2 id="learning-data">学习数据怎样使用</h2>
        <div className="definition-list">
          <div><b>学习画像</b><p>包含你主动提供的背景、目标、经验、偏好和学习节奏，用于调整教材表达与路径。</p></div>
          <div><b>学习证据</b><p>来自测验与 Ask Me 等正式验证，用于调整对各个概念掌握情况的判断。</p></div>
          <div><b>问答与反馈</b><p>绑定到具体学习场景，用于回答问题、处理内容反馈和改进体验。</p></div>
        </div>
        <h2 id="consent">隐私同意</h2>
        <p>在正式填写学习画像前，你需要确认当前版本的隐私告知和自愿参加内测。告知版本变化时，系统会重新说明需要确认的内容，不会静默沿用过期授权。</p>
        <h2 id="leave">退出与删除</h2>
        <p>在“个人中心 → 账号与数据”中可以查看账号与数据状态，并提交退出内测或数据删除申请。界面会说明操作影响、处理状态和下一步。重要状态变化会保留必要审计记录，但不会向普通用户暴露内部日志或敏感运行信息。</p>
      </>
    ),
  },
  {
    slug: 'faq',
    group: '信任与支持',
    title: '常见问题',
    description: '快速找到关于生成、进度、解锁、内容和账号的答案。',
    keywords: 'FAQ 常见问题 为什么 生成 慢 进度 解锁 删除 登录',
    readingTime: '4 分钟',
    content: (
      <div className="faq-list">
        <details open><summary>Slow 和普通 AI 对话有什么不同？</summary><p>普通对话通常围绕当前问题给出一次回答。Slow 把目标组织为连续教材，通过逐节验证形成学习证据，并让后续内容参考你已经掌握或仍然薄弱的概念。</p></details>
        <details><summary>为什么创建目标后不能立刻看到整套正文？</summary><p>Slow 会先规划完整学习路径，再逐步准备正式内容。后续书籍还会根据你在前一本书中的真实学习证据重新校准；一次性生成全部正文会失去这种适应能力。</p></details>
        <details><summary>为什么下一节没有解锁？</summary><p>请先完成当前小节的选择题并达到及格线。若已经及格但状态没有更新，刷新页面后再查看；问题仍存在时使用全局反馈并说明所在系列和小节。</p></details>
        <details><summary>可以跳过我已经会的内容吗？</summary><p>新书生成会参考已有掌握证据，减少对高掌握概念的重复讲解。当前版本仍以正式验证作为解锁依据，不能仅由浏览器手动标记掌握。</p></details>
        <details><summary>Ask AI 的回答会改变正式正文吗？</summary><p>不会。Ask AI 是绑定当前小节和段落的独立会话，不会直接覆盖正式正文。针对正文提交的内容反馈会进入单独的处理流程。</p></details>
        <details><summary>AI 内容都经过事实核验了吗？</summary><p>没有这样的笼统承诺。发布前检查关注内容完整性、学习范围以及题目与正文是否对应；重要事实和高风险决策应继续核对专业的一手来源。</p></details>
        <details><summary>如何报告错误或提出建议？</summary><p>具体正文问题使用段落旁的“反馈这段”；产品问题使用页面边缘的“反馈”。也可以通过 GitHub 仓库提交不包含个人数据和安全细节的公开问题。</p></details>
        <details><summary>如何申请 Alpha 体验？</summary><p>Slow 目前采用邀请制测试。发送邮件至 <a href="mailto:alpha@slow.net.cn">alpha@slow.net.cn</a>，简单说明你想学习的主题；审核后会回复注册邀请码。</p></details>
      </div>
    ),
  },
];

const groups = ['开始使用', '核心概念', '学习方法', '信任与支持'] as const;

function slugFromLocation() {
  const prefix = DOCS_BASE_PATH === '/' ? '/' : `${DOCS_BASE_PATH}/`;
  const slug = window.location.pathname.startsWith(prefix)
    ? window.location.pathname.slice(prefix.length).split('/')[0]
    : '';
  return pages.some((page) => page.slug === slug) ? slug : 'welcome';
}

export default function DocsApp() {
  const [activeSlug, setActiveSlug] = useState(slugFromLocation);
  const [menuOpen, setMenuOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const searchRef = useRef<HTMLInputElement | null>(null);
  const page = pages.find((candidate) => candidate.slug === activeSlug) || pages[0];

  const results = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase('zh-CN');
    if (!normalized) return pages;
    return pages.filter((candidate) => (
      `${candidate.title} ${candidate.description} ${candidate.keywords}`
        .toLocaleLowerCase('zh-CN')
        .includes(normalized)
    ));
  }, [query]);

  const navigate = (slug: string, mode: 'push' | 'replace' = 'push') => {
    const path = docsPath(slug);
    if (window.location.pathname !== path) window.history[mode === 'push' ? 'pushState' : 'replaceState']({}, '', path);
    setActiveSlug(slug);
    setMenuOpen(false);
    setSearchOpen(false);
    setQuery('');
    window.scrollTo({ top: 0, behavior: 'instant' });
  };

  useEffect(() => {
    const onPopState = () => {
      setActiveSlug(slugFromLocation());
      window.scrollTo({ top: 0, behavior: 'instant' });
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setSearchOpen(true);
      }
      if (event.key === 'Escape') setSearchOpen(false);
    };
    window.addEventListener('popstate', onPopState);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('popstate', onPopState);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, []);

  useEffect(() => {
    document.title = `${page.title} · Slow Docs`;
    document.querySelector('meta[name="description"]')?.setAttribute('content', page.description);
  }, [page]);

  useEffect(() => {
    if (!searchOpen) return;
    const frame = window.requestAnimationFrame(() => searchRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [searchOpen]);

  const pageIndex = pages.findIndex((candidate) => candidate.slug === page.slug);

  return (
    <div className="docs-shell">
      <header className="docs-topbar">
        <a className="docs-brand" href={docsPath('welcome')} onClick={(event) => { event.preventDefault(); navigate('welcome'); }}>
          <img src={`${import.meta.env.BASE_URL}slow-mark.svg`} alt="" />
          <b>slow</b><i aria-hidden="true" /><span>使用指南</span>
        </a>
        <div className="docs-top-actions">
          <button className="docs-search-trigger" type="button" onClick={() => setSearchOpen(true)}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
            <span>搜索文档</span><kbd>⌘ K</kbd>
          </button>
          <a href={STANDALONE_DOCS ? 'https://github.com/3321-cpsasd/slow' : `${PUBLIC_BASE || ''}/`}>
            {STANDALONE_DOCS ? '查看源码' : '打开 Slow'} <span aria-hidden="true">→</span>
          </a>
          <button className="docs-menu-button" type="button" aria-label="打开文档目录" aria-expanded={menuOpen} onClick={() => setMenuOpen((value) => !value)}>
            <span /><span />
          </button>
        </div>
      </header>

      <aside className={`docs-sidebar ${menuOpen ? 'is-open' : ''}`}>
        <nav aria-label="文档目录">
          {groups.map((group) => (
            <section key={group}>
              <h2>{group}</h2>
              {pages.filter((item) => item.group === group).map((item) => (
                <a
                  key={item.slug}
                  className={item.slug === page.slug ? 'active' : ''}
                  aria-current={item.slug === page.slug ? 'page' : undefined}
                  href={docsPath(item.slug)}
                  onClick={(event) => { event.preventDefault(); navigate(item.slug); }}
                >
                  <span>{item.title}</span><i aria-hidden="true">{item.slug === page.slug ? '—' : '↗'}</i>
                </a>
              ))}
            </section>
          ))}
        </nav>
        <footer>
          <span>当前版本</span><b>Alpha 指南</b>
          <a href="https://github.com/3321-cpsasd/slow" target="_blank" rel="noreferrer">GitHub <span aria-hidden="true">↗</span></a>
        </footer>
      </aside>
      {menuOpen && <button className="docs-menu-scrim" type="button" aria-label="关闭文档目录" onClick={() => setMenuOpen(false)} />}

      <main className="docs-main" id="main-content">
        <article className="docs-article">
          <header className="docs-article-header">
            <p>{page.group}<span>/</span>{page.readingTime}</p>
            <h1>{page.title}</h1>
            <p>{page.description}</p>
          </header>
          <div className="docs-prose">{page.content}</div>
          <nav className="docs-pagination" aria-label="上一篇和下一篇">
            {pageIndex > 0 ? (
              <a href={docsPath(pages[pageIndex - 1].slug)} onClick={(event) => { event.preventDefault(); navigate(pages[pageIndex - 1].slug); }}>
                <small>上一篇</small><b>← {pages[pageIndex - 1].title}</b>
              </a>
            ) : <span />}
            {pageIndex < pages.length - 1 ? (
              <a href={docsPath(pages[pageIndex + 1].slug)} onClick={(event) => { event.preventDefault(); navigate(pages[pageIndex + 1].slug); }}>
                <small>下一篇</small><b>{pages[pageIndex + 1].title} →</b>
              </a>
            ) : <span />}
          </nav>
          <footer className="docs-article-footer">
            <p>这篇指南解决了你的问题吗？</p>
            <a href="mailto:alpha@slow.net.cn?subject=Slow%20Docs%20%E5%8F%8D%E9%A6%88">告诉我们哪里还不清楚 <span aria-hidden="true">→</span></a>
          </footer>
        </article>
      </main>

      {searchOpen && (
        <div className="docs-search-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSearchOpen(false); }}>
          <section className="docs-search-dialog" role="dialog" aria-modal="true" aria-label="搜索 Slow 使用指南">
            <label>
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
              <input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入关键词，例如“解锁”或“恢复码”" />
              <kbd>ESC</kbd>
            </label>
            <div className="docs-search-results">
              {results.length ? results.map((result) => (
                <button key={result.slug} type="button" onClick={() => navigate(result.slug)}>
                  <span><small>{result.group}</small><b>{result.title}</b><p>{result.description}</p></span><i aria-hidden="true">→</i>
                </button>
              )) : <p className="docs-no-results"><b>没有找到相关内容</b><span>换一个更短的关键词，或发送邮件告诉我们你想了解什么。</span></p>}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
