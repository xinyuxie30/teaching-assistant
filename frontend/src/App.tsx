import { App as AntApp, Button, Form, Input, Tag } from "antd";
import { lazy, Suspense, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiClient } from "./api/client";
import { ErrorNotice, Loading } from "./components/common";
import { BatchList } from "./features/assignments/BatchList";
import type { SavedDraft } from "./features/review/ReviewPage";
import { PreparationSession } from "./features/preparation/session";

const PreparationPage = lazy(() => import("./features/preparation/PreparationPage").then((module) => ({ default: module.PreparationPage })));
const BatchWorkspace = lazy(() => import("./features/assignments/BatchWorkspace").then((module) => ({ default: module.BatchWorkspace })));
const ReviewPage = lazy(() => import("./features/review/ReviewPage").then((module) => ({ default: module.ReviewPage })));
const PracticePage = lazy(() => import("./features/practice/PracticePage").then((module) => ({ default: module.PracticePage })));

interface Session { api: ApiClient; teacherId: string; identityMode: string }
type Route = { page: "batches" | "prepare" | "practice" } | { page: "batch"; batchId: string; tab?: string }
  | { page: "review"; batchId: string; itemId: string; returnTab?: string };

function Connect({ onConnected }: { onConnected: (session: Session) => void }) {
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>();
  const connect = async () => {
    setLoading(true);
    setError(undefined);
    const api = new ApiClient(token.trim());
    try {
      const identity = await api.get<{ teacher_id: string; identity_mode: string }>("/v1/me");
      setToken("");
      onConnected({ api, teacherId: identity.teacher_id, identityMode: identity.identity_mode });
    } catch (failure) { setError(failure); }
    finally { setLoading(false); }
  };
  return <div className="connect-page">
    <section className="connect-intro">
      <div className="brand-mark">教</div><div className="eyebrow">教师工作台</div>
      <h1>把判断留给老师，<br />让批改有据可循。</h1>
      <p>在同一处查看答案、检查评分依据、确认结果，<br />让每一次复核都留下可追溯的记录。</p>
      <div className="connect-steps"><span>01 打开作业批次</span><span>02 逐点评判</span><span>03 保存与导出</span></div>
    </section>
    <section className="connect-card">
      <Tag color="cyan">本地开发版</Tag><h2>连接教学工作区</h2>
      <p className="muted">使用后端配置的开发令牌进入当前老师的工作区。</p>
      <Form layout="vertical" onFinish={() => void connect()}>
        <Form.Item label="开发访问令牌" required>
          <Input.Password value={token} onChange={(event) => setToken(event.target.value)}
            autoComplete="off" aria-label="开发访问令牌" placeholder="输入 TA_DEV_API_TOKEN"
            size="large" disabled={loading} />
        </Form.Item>
        <ErrorNotice error={error} />
        <Button type="primary" size="large" block htmlType="submit" loading={loading} disabled={!token.trim()}>
          进入工作台
        </Button>
      </Form>
      <p className="muted small">令牌只保留在当前页面会话中；刷新页面后需要重新输入。</p>
    </section>
  </div>;
}

export function App() {
  const { modal } = AntApp.useApp();
  const [session, setSession] = useState<Session>();
  const [route, setRoute] = useState<Route>({ page: "batches" });
  const [dirty, setDirty] = useState(false);
  const drafts = useRef(new Map<string, SavedDraft>());
  const [preparation] = useState(() => new PreparationSession());
  const preparationDirty = useSyncExternalStore(preparation.subscribe, preparation.hasDirty);
  const markDirty = useCallback((value: boolean) => setDirty(value), []);
  const anyDraft = () => [...drafts.current.values()].some((saved) =>
    JSON.stringify(saved.draft) !== saved.baseline);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirty || anyDraft() || preparation.hasDirty() || session?.api.hasUnresolvedWrites()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty, session, preparation]);
  const navigate = (next: Route) => {
    const go = () => { setDirty(false); setRoute(next); window.scrollTo({ top: 0 }); };
    if (!dirty && !(route.page === "prepare" && preparationDirty)) { go(); return; }
    modal.confirm({
      title: "暂时离开当前页面？",
      content: "编辑和未确定的提交会保留在当前页面会话中，返回后可继续处理。刷新或关闭页面会丢失这些临时内容。",
      okText: "暂时离开", cancelText: "继续编辑", onOk: go,
    });
  };
  const disconnect = () => {
    const close = () => {
      drafts.current.clear();
      preparation.clear();
      setDirty(false);
      setRoute({ page: "batches" });
      setSession(undefined);
    };
    modal.confirm({
      title: "退出当前工作区？",
      content: dirty || anyDraft() || preparationDirty || session?.api.hasUnresolvedWrites()
        ? "当前有未提交编辑或结果未确定的操作。退出会清除临时内容和提交标识；请先完成提交或重试。"
        : "已保存的记录会继续保留，访问令牌将从页面会话中清除。",
      okText: "退出并清除会话", cancelText: "留在工作区",
      okButtonProps: { danger: true, disabled: session?.api.hasUnresolvedWrites() },
      onOk: close,
    });
  };
  if (!session) return <Connect onConnected={setSession} />;
  return <div className="workbench">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">教</span><span>教学助手<small>教师工作台</small></span></div>
      <div className="sidebar-label">作业管理</div>
      <button className={`nav-item ${route.page === "prepare" ? "selected" : ""}`} onClick={() => navigate({ page: "prepare" })}>
        <span aria-hidden="true">＋</span>作业准备
      </button>
      <button className={`nav-item ${["batches", "batch", "review"].includes(route.page) ? "selected" : ""}`} onClick={() => navigate({ page: "batches" })}>
        <span aria-hidden="true">▤</span>作业与复核
      </button>
      <button className={`nav-item ${route.page === "practice" ? "selected" : ""}`} onClick={() => navigate({ page: "practice" })}>
        <span aria-hidden="true">◎</span>针对性补练
      </button>
      <div className="sidebar-note"><span className="status-dot" />固定规则 · 老师确认</div>
    </aside>
    <div className="main-area">
      <header className="topbar">
        <div className="breadcrumb">教学工作区<span>/</span>{route.page === "review" ? "答案复核"
          : route.page === "batch" ? "批次详情" : route.page === "prepare" ? "作业准备" : route.page === "practice" ? "针对性补练" : "作业批次"}</div>
        <div className="button-row"><Tag>{session.identityMode === "local-development-token" ? "开发身份" : "当前老师"}</Tag>
          <Button type="text" onClick={disconnect}>退出工作区</Button></div>
      </header>
      <main>
        <div hidden={route.page !== "practice"}><Suspense fallback={<Loading />}><PracticePage api={session.api} /></Suspense></div>
        {route.page === "batches" && <BatchList api={session.api}
          onPrepare={() => navigate({ page: "prepare" })}
          onOpen={(batchId) => navigate({ page: "batch", batchId })} />}
        {route.page === "prepare" && <Suspense fallback={<Loading />}><PreparationPage api={session.api} store={preparation}
          onBatch={(batchId) => navigate({ page: "batch", batchId })} /></Suspense>}
        {route.page === "batch" && <BatchWorkspace key={route.batchId} api={session.api} batchId={route.batchId} initialTab={route.tab}
          onBack={() => navigate({ page: "batches" })}
          onReview={(itemId, returnTab) => navigate({ page: "review", batchId: route.batchId, itemId, returnTab })} />}
        {route.page === "review" && <ReviewPage key={route.itemId} api={session.api} itemId={route.itemId}
          drafts={drafts.current} onDirtyChange={markDirty}
          onBack={() => navigate({ page: "batch", batchId: route.batchId, tab: route.returnTab })} />}
      </main>
    </div>
  </div>;
}
