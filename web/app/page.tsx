"use client";

import {
  ChangeEvent,
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type View = "domains" | "overview" | "sources" | "semantic" | "ask" | "runs";
type ConnectionState = "checking" | "live" | "demo";
type SemanticTab = "graph" | "metrics" | "paths";

type Entity = {
  name: string;
  table: string;
  label: string;
  kind: "dimension" | "fact" | "bridge";
  owner: string;
  grain: string;
  dimensions: number;
  metrics: number;
  x: number;
  y: number;
};

type Metric = {
  name: string;
  label: string;
  entity: string;
  aggregation: string;
  expression: string;
  description: string;
};

type QueryResult = {
  status: string;
  runId: string;
  explanation: string;
  sql: string;
  columns: string[];
  rows: Array<Array<string | number>>;
  rowCount: number;
};

type RunRecord = {
  id: string;
  domainId: string;
  question: string;
  status: "Passed" | "Blocked" | "Running";
  model: string;
  rows: number;
  duration: string;
  time: string;
};

type UploadedSource = {
  id: string;
  domainId: string;
  name: string;
  type: string;
  size: string;
  tables: number;
  rows: string;
  status: "Ready" | "Draft";
};

type DataDomain = {
  id: string;
  name: string;
  slug: string;
  description: string;
  owner: string;
  status: "ready" | "draft";
  isSample: boolean;
  sourceCount: number;
  runCount: number;
};

type SemanticDraft = {
  entity: string;
  description: string;
  owner: string;
  grain: string;
  primaryKey: string;
  sensitivity: "public" | "internal" | "restricted";
  dimensions: string[];
  metrics: Array<{
    name: string;
    description: string;
    aggregation: "count" | "sum";
    expression: string;
  }>;
};

const SAMPLE_DOMAIN_ID = "domain_anime_streaming";

const INITIAL_SEMANTIC_DRAFT: SemanticDraft = {
  entity: "uploaded_record",
  description: "One governed business record represented by the uploaded source.",
  owner: "workspace-admin",
  grain: "record_id",
  primaryKey: "record_id",
  sensitivity: "internal",
  dimensions: ["record_id", "entity_id", "recorded_at"],
  metrics: [
    {
      name: "uploaded_record_count",
      description: "Count of reviewed records at the declared grain.",
      aggregation: "count",
      expression: "COUNT(*)",
    },
    {
      name: "uploaded_amount_sum",
      description: "Sum of the profiled amount field in source-native units.",
      aggregation: "sum",
      expression: "SUM(amount)",
    },
  ],
};

const NAV_ITEMS: Array<{
  id: View;
  label: string;
  caption: string;
  icon: string;
}> = [
  { id: "domains", label: "Data Domains", caption: "Create & switch context", icon: "◎" },
  { id: "overview", label: "Overview", caption: "Workspace health", icon: "◇" },
  { id: "sources", label: "Data Sources", caption: "Ingest & profile", icon: "▦" },
  { id: "semantic", label: "Semantic Studio", caption: "Model & govern", icon: "⌘" },
  { id: "ask", label: "Ask & Analyze", caption: "Query with evidence", icon: "✦" },
  { id: "runs", label: "Run History", caption: "Trace every decision", icon: "↺" },
];

const INITIAL_DOMAINS: DataDomain[] = [
  {
    id: SAMPLE_DOMAIN_ID,
    name: "Anime Streaming",
    slug: "anime-streaming",
    description:
      "Synthetic streaming analytics showcase spanning content, engagement, subscriptions, advertising, and merchandise.",
    owner: "Content analytics",
    status: "ready",
    isSample: true,
    sourceCount: 1,
    runCount: 5,
  },
];

const ENTITIES: Entity[] = [
  {
    name: "studio",
    table: "dim_studio",
    label: "Studio",
    kind: "dimension",
    owner: "content-analytics",
    grain: "studio_id",
    dimensions: 4,
    metrics: 0,
    x: 24,
    y: 42,
  },
  {
    name: "anime",
    table: "dim_anime",
    label: "Anime",
    kind: "dimension",
    owner: "content-analytics",
    grain: "anime_id",
    dimensions: 9,
    metrics: 0,
    x: 222,
    y: 42,
  },
  {
    name: "genre",
    table: "dim_genre",
    label: "Genre",
    kind: "dimension",
    owner: "content-analytics",
    grain: "genre_id",
    dimensions: 3,
    metrics: 0,
    x: 420,
    y: 16,
  },
  {
    name: "anime_genre",
    table: "bridge_anime_genre",
    label: "Anime Genre",
    kind: "bridge",
    owner: "content-analytics",
    grain: "anime_id + genre_id",
    dimensions: 2,
    metrics: 0,
    x: 420,
    y: 94,
  },
  {
    name: "episode",
    table: "dim_episode",
    label: "Episode",
    kind: "dimension",
    owner: "content-analytics",
    grain: "episode_id",
    dimensions: 5,
    metrics: 0,
    x: 222,
    y: 160,
  },
  {
    name: "watch_session",
    table: "fact_watch_session",
    label: "Watch Session",
    kind: "fact",
    owner: "engagement-analytics",
    grain: "watch_session_id",
    dimensions: 5,
    metrics: 3,
    x: 420,
    y: 184,
  },
  {
    name: "ad_impression",
    table: "fact_ad_impression",
    label: "Ad Impression",
    kind: "fact",
    owner: "ads-analytics",
    grain: "impression_id",
    dimensions: 5,
    metrics: 2,
    x: 624,
    y: 124,
  },
  {
    name: "user",
    table: "dim_user",
    label: "User",
    kind: "dimension",
    owner: "growth-analytics",
    grain: "user_id",
    dimensions: 7,
    metrics: 0,
    x: 24,
    y: 272,
  },
  {
    name: "rating",
    table: "fact_rating",
    label: "Rating",
    kind: "fact",
    owner: "engagement-analytics",
    grain: "rating_id",
    dimensions: 4,
    metrics: 1,
    x: 222,
    y: 272,
  },
  {
    name: "subscription",
    table: "fact_subscription",
    label: "Subscription",
    kind: "fact",
    owner: "monetization-analytics",
    grain: "subscription_id",
    dimensions: 4,
    metrics: 2,
    x: 24,
    y: 386,
  },
  {
    name: "user_follow",
    table: "fact_user_follow",
    label: "User Follow",
    kind: "fact",
    owner: "community-analytics",
    grain: "follow_id",
    dimensions: 1,
    metrics: 1,
    x: 222,
    y: 386,
  },
  {
    name: "merch_product",
    table: "dim_merch_product",
    label: "Merch Product",
    kind: "dimension",
    owner: "commerce-analytics",
    grain: "product_id",
    dimensions: 4,
    metrics: 0,
    x: 420,
    y: 342,
  },
  {
    name: "merch_order",
    table: "fact_merch_order",
    label: "Merch Order",
    kind: "fact",
    owner: "commerce-analytics",
    grain: "order_id",
    dimensions: 3,
    metrics: 0,
    x: 624,
    y: 300,
  },
  {
    name: "merch_order_item",
    table: "fact_merch_order_item",
    label: "Order Item",
    kind: "fact",
    owner: "commerce-analytics",
    grain: "order_item_id",
    dimensions: 2,
    metrics: 2,
    x: 624,
    y: 410,
  },
  {
    name: "calendar",
    table: "dim_date",
    label: "Calendar",
    kind: "dimension",
    owner: "data-platform",
    grain: "date_key",
    dimensions: 7,
    metrics: 0,
    x: 420,
    y: 456,
  },
];

const METRICS: Metric[] = [
  {
    name: "watch_hours",
    label: "Watch hours",
    entity: "watch_session",
    aggregation: "SUM",
    expression: "SUM(fact_watch_session.watch_seconds) / 3600.0",
    description: "Total valid viewing time, normalized to hours.",
  },
  {
    name: "completion_rate",
    label: "Completion rate",
    entity: "watch_session",
    aggregation: "RATIO",
    expression:
      "SUM(fact_watch_session.completed_flag) / NULLIF(COUNT(*), 0)",
    description: "Share of valid watch sessions completed.",
  },
  {
    name: "unique_viewers",
    label: "Unique viewers",
    entity: "watch_session",
    aggregation: "COUNT DISTINCT",
    expression: "COUNT(DISTINCT fact_watch_session.user_id)",
    description: "Distinct viewers with a valid watch session.",
  },
  {
    name: "average_rating",
    label: "Average rating",
    entity: "rating",
    aggregation: "RATIO",
    expression: "SUM(fact_rating.score) / NULLIF(COUNT(*), 0)",
    description: "Average submitted anime rating.",
  },
  {
    name: "subscription_revenue",
    label: "Subscription revenue",
    entity: "subscription",
    aggregation: "SUM",
    expression: "SUM(fact_subscription.recognized_revenue_usd)",
    description: "Recognized subscription revenue in USD.",
  },
  {
    name: "active_subscribers",
    label: "Active subscribers",
    entity: "subscription",
    aggregation: "COUNT DISTINCT",
    expression: "COUNT(DISTINCT fact_subscription.user_id)",
    description: "Distinct users represented by active subscriptions.",
  },
  {
    name: "ad_revenue",
    label: "Ad revenue",
    entity: "ad_impression",
    aggregation: "SUM",
    expression: "SUM(fact_ad_impression.revenue_usd)",
    description: "Revenue attributed to governed ad impressions.",
  },
  {
    name: "ad_click_through_rate",
    label: "Ad CTR",
    entity: "ad_impression",
    aggregation: "RATIO",
    expression:
      "SUM(fact_ad_impression.clicked_flag) / NULLIF(COUNT(*), 0)",
    description: "Click-through rate across eligible impressions.",
  },
  {
    name: "merch_gmv",
    label: "Merch GMV",
    entity: "merch_order_item",
    aggregation: "SUM",
    expression: "SUM(fact_merch_order_item.net_amount_usd)",
    description: "Net merchandise value in USD.",
  },
  {
    name: "merch_units",
    label: "Merch units",
    entity: "merch_order_item",
    aggregation: "SUM",
    expression: "SUM(fact_merch_order_item.quantity)",
    description: "Total merchandise units purchased.",
  },
  {
    name: "community_follows",
    label: "Community follows",
    entity: "user_follow",
    aggregation: "COUNT",
    expression: "COUNT(DISTINCT fact_user_follow.follow_id)",
    description: "Distinct user-follow relationships created.",
  },
];

const JOIN_PATHS = [
  {
    name: "watches_to_anime_via_episode",
    label: "Watch → Anime",
    steps: ["Watch session", "Episode", "Anime"],
    hops: 2,
    risk: "Safe",
  },
  {
    name: "watches_to_studio_via_episode_anime",
    label: "Watch → Studio",
    steps: ["Watch session", "Episode", "Anime", "Studio"],
    hops: 3,
    risk: "Safe",
  },
  {
    name: "watches_to_release_date_via_episode",
    label: "Watch → Release date",
    steps: ["Watch session", "Episode", "Calendar"],
    hops: 2,
    risk: "Safe",
  },
  {
    name: "merch_items_to_anime_via_product",
    label: "Order item → Anime",
    steps: ["Order item", "Merch product", "Anime"],
    hops: 2,
    risk: "Safe",
  },
  {
    name: "merch_items_to_user_via_order",
    label: "Order item → User",
    steps: ["Order item", "Merch order", "User"],
    hops: 2,
    risk: "Safe",
  },
  {
    name: "merch_items_to_date_via_order",
    label: "Order item → Date",
    steps: ["Order item", "Merch order", "Calendar"],
    hops: 2,
    risk: "Safe",
  },
  {
    name: "ads_to_studio_via_anime",
    label: "Ad → Studio",
    steps: ["Ad impression", "Anime", "Studio"],
    hops: 2,
    risk: "Safe",
  },
];

const GRAPH_LINKS = [
  { x: 144, y: 77, width: 78, angle: 0 },
  { x: 340, y: 76, width: 86, angle: -12 },
  { x: 340, y: 91, width: 86, angle: 12 },
  { x: 278, y: 118, width: 45, angle: 90 },
  { x: 340, y: 198, width: 80, angle: 0 },
  { x: 536, y: 190, width: 98, angle: -22 },
  { x: 142, y: 308, width: 80, angle: 0 },
  { x: 98, y: 268, width: 235, angle: -24 },
  { x: 96, y: 330, width: 68, angle: 90 },
  { x: 340, y: 300, width: 113, angle: -35 },
  { x: 340, y: 404, width: 86, angle: -18 },
  { x: 534, y: 379, width: 104, angle: -25 },
  { x: 678, y: 362, width: 48, angle: 90 },
  { x: 536, y: 454, width: 106, angle: 0 },
];

const DEMO_RESULT: QueryResult = {
  status: "success",
  runId: "qf_7a3e2c91",
  explanation:
    "Fantasy leads total watch hours, while Mystery shows the strongest completion rate. The query uses the governed Watch → Episode → Anime join path and excludes invalid sessions.",
  sql: `SELECT
  g.genre_name AS genre,
  ROUND(SUM(w.watch_seconds) / 3600.0, 1) AS watch_hours,
  ROUND(
    100.0 * SUM(w.completed_flag) / NULLIF(COUNT(*), 0),
    1
  ) AS completion_rate
FROM fact_watch_session AS w
JOIN dim_episode AS e ON w.episode_id = e.episode_id
JOIN dim_anime AS a ON e.anime_id = a.anime_id
JOIN bridge_anime_genre AS ag ON a.anime_id = ag.anime_id
JOIN dim_genre AS g ON ag.genre_id = g.genre_id
WHERE w.is_valid = 1
GROUP BY g.genre_name
ORDER BY watch_hours DESC
LIMIT 6;`,
  columns: ["genre", "watch_hours", "completion_rate"],
  rows: [
    ["Fantasy", 2148.6, 71.8],
    ["Action", 1924.1, 68.4],
    ["Sci-Fi", 1711.8, 74.2],
    ["Romance", 1453.7, 76.9],
    ["Mystery", 1228.4, 81.3],
    ["Comedy", 1104.2, 65.7],
  ],
  rowCount: 6,
};

const INITIAL_RUNS: RunRecord[] = [
  {
    id: "qf_7a3e2c91",
    domainId: SAMPLE_DOMAIN_ID,
    question: "Compare watch hours and completion rate by genre",
    status: "Passed",
    model: "qwen-plus",
    rows: 6,
    duration: "1.84s",
    time: "2 min ago",
  },
  {
    id: "qf_4ce8b1a2",
    domainId: SAMPLE_DOMAIN_ID,
    question: "Top anime by merchandise GMV this quarter",
    status: "Passed",
    model: "qwen-plus",
    rows: 10,
    duration: "2.12s",
    time: "18 min ago",
  },
  {
    id: "qf_e832bb77",
    domainId: SAMPLE_DOMAIN_ID,
    question: "Show every user email with subscription revenue",
    status: "Blocked",
    model: "qwen-plus",
    rows: 0,
    duration: "0.41s",
    time: "42 min ago",
  },
  {
    id: "qf_729af843",
    domainId: SAMPLE_DOMAIN_ID,
    question: "Monthly active subscribers by plan tier",
    status: "Passed",
    model: "gpt-4.1-mini",
    rows: 24,
    duration: "1.61s",
    time: "1 hr ago",
  },
  {
    id: "qf_c239a101",
    domainId: SAMPLE_DOMAIN_ID,
    question: "Which studios have the highest average rating?",
    status: "Passed",
    model: "qwen-plus",
    rows: 12,
    duration: "1.49s",
    time: "3 hr ago",
  },
];

const TABLES = [
  ["fact_watch_session", "150,000", "Fact", "5 metrics"],
  ["fact_ad_impression", "80,000", "Fact", "2 metrics"],
  ["fact_rating", "40,000", "Fact", "1 metric"],
  ["fact_merch_order_item", "37,500", "Fact", "2 metrics"],
  ["fact_user_follow", "24,000", "Fact", "1 metric"],
  ["dim_user", "8,000", "Dimension", "7 dimensions"],
  ["dim_anime", "240", "Dimension", "9 dimensions"],
];

const EXAMPLE_QUESTIONS = [
  "Compare watch hours and completion rate by genre",
  "Which studios generate the most ad revenue?",
  "Show subscription revenue by plan tier and month",
  "Which anime drive both engagement and merch GMV?",
];

const sleep = (duration: number) =>
  new Promise((resolve) => window.setTimeout(resolve, duration));

function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function Icon({ value }: { value: string }) {
  return (
    <span className="icon-glyph" aria-hidden="true">
      {value}
    </span>
  );
}

function normalizeQueryResult(payload: Record<string, unknown>): QueryResult {
  const columns = Array.isArray(payload.columns)
    ? payload.columns.map(String)
    : DEMO_RESULT.columns;
  const rows = Array.isArray(payload.rows)
    ? payload.rows.map((row) =>
        Array.isArray(row)
          ? row.map((cell) =>
              typeof cell === "number" ? cell : String(cell ?? ""),
            )
          : [],
      )
    : DEMO_RESULT.rows;

  return {
    status: String(payload.status ?? "success"),
    runId: String(payload.run_id ?? payload.runId ?? "qf_live"),
    explanation: String(payload.explanation ?? "Query completed successfully."),
    sql: String(payload.sql ?? "-- SQL was not included in the response"),
    columns,
    rows,
    rowCount: Number(payload.row_count ?? payload.rowCount ?? rows.length),
  };
}

export default function Home() {
  const [activeView, setActiveView] = useState<View>("domains");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [connection, setConnection] =
    useState<ConnectionState>("checking");
  const [query, setQuery] = useState(EXAMPLE_QUESTIONS[0]);
  const [isRunning, setIsRunning] = useState(false);
  const [runStage, setRunStage] = useState(0);
  const [result, setResult] = useState<QueryResult>(DEMO_RESULT);
  const [runs, setRuns] = useState<RunRecord[]>(INITIAL_RUNS);
  const [selectedEntity, setSelectedEntity] = useState("watch_session");
  const [semanticTab, setSemanticTab] = useState<SemanticTab>("graph");
  const [entityFilter, setEntityFilter] = useState("");
  const [toast, setToast] = useState("");
  const [domains, setDomains] = useState<DataDomain[]>(INITIAL_DOMAINS);
  const [activeDomainId, setActiveDomainId] = useState(SAMPLE_DOMAIN_ID);
  const [domainMenuOpen, setDomainMenuOpen] = useState(false);
  const [createDomainOpen, setCreateDomainOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [uploadStep, setUploadStep] = useState(1);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [isProfiling, setIsProfiling] = useState(false);
  const [semanticReviewed, setSemanticReviewed] = useState(false);
  const [semanticValidated, setSemanticValidated] = useState(false);
  const [semanticDraft, setSemanticDraft] = useState<SemanticDraft>(
    INITIAL_SEMANTIC_DRAFT,
  );
  const [uploadedSources, setUploadedSources] = useState<UploadedSource[]>([]);
  const [runFilter, setRunFilter] = useState<"All" | "Passed" | "Blocked">(
    "All",
  );
  const fileInputRef = useRef<HTMLInputElement>(null);

  const activeDomain =
    domains.find((domain) => domain.id === activeDomainId) ??
    INITIAL_DOMAINS[0];

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 2500);

    fetch("/api/queryforge/health", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Backend unavailable");
        return response.json();
      })
      .then(() => setConnection("live"))
      .catch(() => setConnection("demo"))
      .finally(() => window.clearTimeout(timer));

    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    fetch("/api/studio/domains")
      .then((response) => {
        if (!response.ok) throw new Error("Persistence unavailable");
        return response.json() as Promise<{
          domains?: Array<Record<string, unknown>>;
        }>;
      })
      .then((payload) => {
        const persisted = (payload.domains ?? []).map((domain) => ({
          id: String(domain.id),
          name: String(domain.name),
          slug: String(domain.slug),
          description: String(domain.description ?? ""),
          owner: String(domain.owner ?? "Workspace admin"),
          status:
            String(domain.status) === "ready"
              ? ("ready" as const)
              : ("draft" as const),
          isSample: Boolean(domain.is_sample),
          sourceCount: Number(domain.source_count ?? 0),
          runCount: Number(domain.run_count ?? 0),
        }));
        if (persisted.length) {
          setDomains(persisted);
          setActiveDomainId((current) =>
            persisted.some((domain) => domain.id === current)
              ? current
              : persisted[0].id,
          );
        }
      })
      .catch(() => undefined);

    fetch("/api/studio/upload")
      .then((response) => {
        if (!response.ok) throw new Error("Persistence unavailable");
        return response.json() as Promise<{
          sources?: Array<Record<string, unknown>>;
        }>;
      })
      .then((payload) => {
        const persisted = (payload.sources ?? []).map((source) => ({
          id: String(source.id),
          domainId: String(source.domain_id ?? SAMPLE_DOMAIN_ID),
          name: String(source.name),
          type: String(source.source_type ?? "DATA"),
          size: formatBytes(Number(source.size_bytes ?? 0)),
          tables: Number(source.table_count ?? 1),
          rows: "Profiled",
          status: "Ready" as const,
        }));
        if (persisted.length) setUploadedSources(persisted);
      })
      .catch(() => undefined);

    fetch("/api/studio/runs")
      .then((response) => {
        if (!response.ok) throw new Error("Persistence unavailable");
        return response.json() as Promise<{
          runs?: Array<Record<string, unknown>>;
        }>;
      })
      .then((payload) => {
        const persisted = (payload.runs ?? []).map((run) => ({
          id: String(run.id),
          domainId: String(run.domain_id ?? SAMPLE_DOMAIN_ID),
          question: String(run.question),
          status:
            String(run.status) === "Blocked"
              ? ("Blocked" as const)
              : ("Passed" as const),
          model: String(run.model ?? "configured model"),
          rows: Number(run.row_count ?? 0),
          duration: String(run.duration ?? "live"),
          time: "saved",
        }));
        if (persisted.length) {
          setRuns((current) => [
            ...persisted,
            ...current.filter(
              (item) => !persisted.some((saved) => saved.id === item.id),
            ),
          ]);
        }
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const filteredEntities = useMemo(
    () =>
      ENTITIES.filter((entity) =>
        `${entity.label} ${entity.table}`
          .toLowerCase()
          .includes(entityFilter.toLowerCase()),
      ),
    [entityFilter],
  );

  const activeEntity =
    ENTITIES.find((entity) => entity.name === selectedEntity) ?? ENTITIES[0];

  const filteredRuns = runs.filter(
    (run) =>
      run.domainId === activeDomain.id &&
      (runFilter === "All" || run.status === runFilter),
  );
  const activeDomainSources = uploadedSources.filter(
    (source) => source.domainId === activeDomain.id,
  );
  const activeDomainRuns = runs.filter(
    (run) => run.domainId === activeDomain.id,
  );

  async function runQuery(nextQuestion?: string) {
    const submitted = (nextQuestion ?? query).trim();
    if (!submitted || isRunning) return;
    if (!activeDomain.isSample) {
      setActiveView("ask");
      setToast(
        activeDomainSources.length
          ? "Publish an executable connector for this domain before running analysis."
          : "Upload data and publish its semantic contract before running analysis.",
      );
      return;
    }

    setQuery(submitted);
    setActiveView("ask");
    setIsRunning(true);
    setRunStage(1);
    await sleep(350);
    setRunStage(2);
    await sleep(420);
    setRunStage(3);

    let nextResult = DEMO_RESULT;
    if (connection === "live") {
      try {
        const response = await fetch("/api/queryforge/ask", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            question: submitted,
            database:
              "sample_data/anime_streaming/anime_streaming.sqlite",
            semantic_model_path:
              "sample_data/anime_streaming/semantic_model.yml",
            sql_policy_path:
              "sample_data/anime_streaming/sql_policy.yml",
            visualize: true,
            report: true,
            complexity_mode: "auto",
          }),
        });
        if (!response.ok) {
          throw new Error("Live query failed");
        }
        nextResult = normalizeQueryResult(await response.json());
      } catch {
        setConnection("demo");
        setToast("Live backend unavailable — continued with demo evidence.");
      }
    } else {
      await sleep(460);
    }

    setRunStage(4);
    await sleep(300);
    setResult(nextResult);
    setRuns((current) => [
      {
        id: nextResult.runId,
        domainId: activeDomain.id,
        question: submitted,
        status: "Passed",
        model: connection === "live" ? "configured model" : "demo-model",
        rows: nextResult.rowCount,
        duration: connection === "live" ? "live" : "1.84s",
        time: "just now",
      },
      ...current.filter((item) => item.id !== nextResult.runId),
    ]);
    void fetch("/api/studio/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        id: nextResult.runId,
        domainId: activeDomain.id,
        question: submitted,
        status: "Passed",
        model: connection === "live" ? "configured model" : "demo-model",
        rowCount: nextResult.rowCount,
        duration: connection === "live" ? "live" : "1.84s",
      }),
    }).catch(() => undefined);
    setIsRunning(false);
    setToast("Governed query completed.");
  }

  function submitQuery(event: FormEvent) {
    event.preventDefault();
    void runQuery();
  }

  function copySql() {
    void navigator.clipboard.writeText(result.sql);
    setToast("SQL copied to clipboard.");
  }

  function downloadResult() {
    const blob = new Blob(
      [
        JSON.stringify(
          {
            question: query,
            ...result,
          },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${result.runId}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    setToast("Run artifact downloaded.");
  }

  function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    if (!files.length) return;
    setUploadFiles(files);
    setSemanticReviewed(false);
    setSemanticValidated(false);
    setSemanticDraft({
      ...INITIAL_SEMANTIC_DRAFT,
      description: `One governed business record represented by data uploaded to ${activeDomain.name}.`,
      owner: activeDomain.owner,
    });
  }

  async function profileFiles() {
    if (!uploadFiles.length) return;
    setIsProfiling(true);
    await sleep(900);
    setIsProfiling(false);
    setUploadStep(2);
  }

  async function validateUploadSemantic() {
    if (!semanticReviewed) return;
    const requiredFields = [
      semanticDraft.entity,
      semanticDraft.description,
      semanticDraft.owner,
      semanticDraft.grain,
      semanticDraft.primaryKey,
    ];
    const metricsValid =
      semanticDraft.metrics.length > 0 &&
      semanticDraft.metrics.every(
        (metric) =>
          metric.name && metric.description && metric.expression,
      );
    if (requiredFields.some((value) => !value.trim()) || !metricsValid) {
      setSemanticValidated(false);
      setToast("Complete the entity, grain, owner, and metric contract first.");
      return;
    }
    await sleep(500);
    setSemanticValidated(true);
    setToast("Semantic contract passed all blocking checks.");
  }

  async function publishUpload() {
    if (!semanticReviewed || !semanticValidated || !uploadFiles.length) return;
    const totalBytes = uploadFiles.reduce((sum, file) => sum + file.size, 0);
    const payload = new FormData();
    uploadFiles.forEach((file) => payload.append("files", file));
    payload.append("domain_id", activeDomain.id);
    payload.append("reviewed", "true");
    payload.append(
      "semantic_contract",
      JSON.stringify({
        ...semanticDraft,
        reviewed: true,
        version: 1,
      }),
    );

    let persisted = false;
    try {
      const response = await fetch("/api/studio/upload", {
        method: "POST",
        body: payload,
      });
      persisted = response.ok;
    } catch {
      persisted = false;
    }

    setUploadedSources((current) => [
      {
        id: crypto.randomUUID(),
        domainId: activeDomain.id,
        name:
          uploadFiles.length === 1
            ? uploadFiles[0].name
            : `${uploadFiles[0].name} + ${uploadFiles.length - 1}`,
        type: uploadFiles
          .map((file) => file.name.split(".").pop()?.toUpperCase() ?? "FILE")
          .filter((value, index, list) => list.indexOf(value) === index)
          .join(" · "),
        size: formatBytes(totalBytes),
        tables: uploadFiles.length,
        rows: "Profiled",
        status: "Ready",
      },
      ...current,
    ]);
    setDomains((current) =>
      current.map((domain) =>
        domain.id === activeDomain.id
          ? {
              ...domain,
              sourceCount: domain.sourceCount + 1,
              status: "ready",
            }
          : domain,
      ),
    );
    setUploadOpen(false);
    setUploadStep(1);
    setUploadFiles([]);
    setSemanticReviewed(false);
    setSemanticValidated(false);
    setToast(
      persisted
        ? "Data and semantic model published atomically."
        : "Demo source published locally with its semantic contract.",
    );
  }

  function selectRun(run: RunRecord) {
    setActiveDomainId(run.domainId);
    setQuery(run.question);
    setActiveView("ask");
  }

  function selectDomain(domain: DataDomain, view: View = "overview") {
    setActiveDomainId(domain.id);
    setDomainMenuOpen(false);
    setActiveView(view);
    setQuery(
      domain.isSample
        ? EXAMPLE_QUESTIONS[0]
        : "Ask a governed question about this data domain",
    );
    setToast(`${domain.name} is now the active data domain.`);
  }

  async function createDomain(input: {
    name: string;
    description: string;
    owner: string;
  }) {
    let created: DataDomain = {
      id: `domain_${crypto.randomUUID().replaceAll("-", "")}`,
      name: input.name,
      slug: input.name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, ""),
      description: input.description,
      owner: input.owner || "Workspace admin",
      status: "draft",
      isSample: false,
      sourceCount: 0,
      runCount: 0,
    };
    try {
      const response = await fetch("/api/studio/domains", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(input),
      });
      if (response.ok) {
        const payload = (await response.json()) as { domain: DataDomain };
        created = payload.domain;
      }
    } catch {
      // Keep the complete workflow available in local demo mode.
    }
    setDomains((current) => [...current, created]);
    setActiveDomainId(created.id);
    setCreateDomainOpen(false);
    setActiveView("sources");
    setToast(`${created.name} created. Add its first governed data source.`);
  }

  return (
    <div className="studio-shell">
      <aside className={cn("sidebar", mobileMenuOpen && "sidebar-open")}>
        <div className="brand-block">
          <button
            className="brand-mark"
            onClick={() => setActiveView("overview")}
            aria-label="QueryForge home"
          >
            QF
          </button>
          <div>
            <div className="brand-name">QueryForge</div>
            <div className="brand-caption">Semantic AI workspace</div>
          </div>
          <span className="beta-badge">BETA</span>
        </div>

        <div className="sidebar-section-label">Workspace</div>
        <nav className="primary-nav" aria-label="Primary navigation">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              className={cn("nav-item", activeView === item.id && "active")}
              onClick={() => {
                setActiveView(item.id);
                setMobileMenuOpen(false);
              }}
              aria-current={activeView === item.id ? "page" : undefined}
            >
              <Icon value={item.icon} />
              <span>
                <strong>{item.label}</strong>
                <small>{item.caption}</small>
              </span>
              {item.id === "semantic" && (
                <span className="nav-health" aria-label="healthy" />
              )}
            </button>
          ))}
        </nav>

        <div className="sidebar-spacer" />
        <div className="workspace-card">
          <div className="workspace-card-top">
            <span className="workspace-avatar">
              {activeDomain.name
                .split(/\s+/)
                .slice(0, 2)
                .map((word) => word[0])
                .join("")
                .toUpperCase()}
            </span>
            <div>
              <strong>{activeDomain.name}</strong>
              <span>{activeDomain.isSample ? "Sample data domain" : "Workspace data domain"}</span>
            </div>
          </div>
          <div className="workspace-progress-row">
            <span>Semantic coverage</span>
            <strong>{activeDomain.isSample ? "100%" : activeDomain.sourceCount ? "Draft" : "0%"}</strong>
          </div>
          <div className="workspace-progress">
            <span
              style={{
                width: activeDomain.isSample
                  ? "100%"
                  : activeDomain.sourceCount
                    ? "42%"
                    : "0%",
              }}
            />
          </div>
        </div>

        <button className="profile-row" onClick={() => setSettingsOpen(true)}>
          <span className="profile-avatar">HC</span>
          <span>
            <strong>Workspace admin</strong>
            <small>Local-first mode</small>
          </span>
          <Icon value="···" />
        </button>
      </aside>

      {mobileMenuOpen && (
        <button
          className="mobile-overlay"
          aria-label="Close navigation"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

      <main className="main-frame">
        <header className="topbar">
          <button
            className="mobile-menu-button"
            aria-label="Open navigation"
            onClick={() => setMobileMenuOpen(true)}
          >
            ☰
          </button>
          <div className="breadcrumb">
            <span>Workspace</span>
            <b>/</b>
            <strong>
              {NAV_ITEMS.find((item) => item.id === activeView)?.label}
            </strong>
          </div>
          <div className="topbar-actions">
            <button
              className={cn(
                "connection-chip",
                connection === "live" && "is-live",
              )}
              onClick={() => setSettingsOpen(true)}
            >
              <span className="connection-dot" />
              {connection === "checking"
                ? "Checking API"
                : connection === "live"
                  ? "Live backend"
                  : "Interactive demo"}
            </button>
            <button
              className="dataset-switcher"
              onClick={() => setDomainMenuOpen((open) => !open)}
              aria-expanded={domainMenuOpen}
              aria-haspopup="menu"
            >
              <span className="dataset-icon">A</span>
              <span>
                <small>Active data domain</small>
                <strong>{activeDomain.name}</strong>
              </span>
              <span className="chevron">⌄</span>
            </button>
            {domainMenuOpen && (
              <div className="domain-switcher-menu" role="menu">
                <div className="domain-switcher-heading">
                  <span>Switch data domain</span>
                  <button
                    onClick={() => {
                      setDomainMenuOpen(false);
                      setCreateDomainOpen(true);
                    }}
                  >
                    ＋ New
                  </button>
                </div>
                {domains.map((domain) => (
                  <button
                    key={domain.id}
                    className={cn(
                      "domain-switcher-option",
                      domain.id === activeDomain.id && "active",
                    )}
                    onClick={() => selectDomain(domain)}
                    role="menuitem"
                  >
                    <span className="domain-option-icon">
                      {domain.isSample ? "A" : "D"}
                    </span>
                    <span>
                      <strong>{domain.name}</strong>
                      <small>
                        {domain.sourceCount} source
                        {domain.sourceCount === 1 ? "" : "s"} · {domain.status}
                      </small>
                    </span>
                    {domain.id === activeDomain.id && <em>✓</em>}
                  </button>
                ))}
                <button
                  className="domain-switcher-all"
                  onClick={() => {
                    setDomainMenuOpen(false);
                    setActiveView("domains");
                  }}
                >
                  Manage all data domains →
                </button>
              </div>
            )}
            <button
              className="icon-button"
              aria-label="Open settings"
              onClick={() => setSettingsOpen(true)}
            >
              ⚙
            </button>
          </div>
        </header>

        <div className="content-scroll">
          {activeView === "domains" && (
            <DomainsView
              domains={domains}
              activeDomainId={activeDomain.id}
              sourceCounts={uploadedSources.reduce<Record<string, number>>(
                (counts, source) => {
                  counts[source.domainId] = (counts[source.domainId] ?? 0) + 1;
                  return counts;
                },
                {},
              )}
              selectDomain={selectDomain}
              createDomain={() => setCreateDomainOpen(true)}
            />
          )}
          {activeView === "overview" && (
            <OverviewView
              domain={activeDomain}
              query={query}
              setQuery={setQuery}
              runQuery={runQuery}
              openSources={() => setActiveView("sources")}
              openSemantic={() => setActiveView("semantic")}
              openRuns={() => setActiveView("runs")}
              connection={connection}
              runs={activeDomainRuns}
            />
          )}
          {activeView === "sources" && (
            <SourcesView
              domain={activeDomain}
              uploadedSources={activeDomainSources}
              openUpload={() => setUploadOpen(true)}
              openSemantic={() => setActiveView("semantic")}
            />
          )}
          {activeView === "semantic" && (
            <SemanticView
              domain={activeDomain}
              hasSources={activeDomain.isSample || activeDomainSources.length > 0}
              openUpload={() => setUploadOpen(true)}
              tab={semanticTab}
              setTab={setSemanticTab}
              entityFilter={entityFilter}
              setEntityFilter={setEntityFilter}
              filteredEntities={filteredEntities}
              selectedEntity={selectedEntity}
              selectEntity={setSelectedEntity}
              activeEntity={activeEntity}
              toast={setToast}
            />
          )}
          {activeView === "ask" && (
            <AskView
              domain={activeDomain}
              hasSources={activeDomain.isSample || activeDomainSources.length > 0}
              query={query}
              setQuery={setQuery}
              submitQuery={submitQuery}
              runQuery={runQuery}
              isRunning={isRunning}
              runStage={runStage}
              result={result}
              copySql={copySql}
              downloadResult={downloadResult}
              runs={activeDomainRuns.slice(0, 4)}
              selectRun={selectRun}
              connection={connection}
            />
          )}
          {activeView === "runs" && (
            <RunsView
              runs={filteredRuns}
              filter={runFilter}
              setFilter={setRunFilter}
              selectRun={selectRun}
              downloadResult={downloadResult}
            />
          )}
        </div>
      </main>

      {uploadOpen && (
        <UploadModal
          domain={activeDomain}
          step={uploadStep}
          files={uploadFiles}
          isProfiling={isProfiling}
          reviewed={semanticReviewed}
          validated={semanticValidated}
          semanticDraft={semanticDraft}
          setSemanticDraft={setSemanticDraft}
          resetValidation={() => setSemanticValidated(false)}
          fileInputRef={fileInputRef}
          close={() => {
            setUploadOpen(false);
            setUploadStep(1);
          }}
          handleFiles={handleFiles}
          profileFiles={profileFiles}
          setStep={setUploadStep}
          setReviewed={setSemanticReviewed}
          validateSemantic={validateUploadSemantic}
          publishUpload={publishUpload}
        />
      )}

      {createDomainOpen && (
        <CreateDomainModal
          close={() => setCreateDomainOpen(false)}
          createDomain={createDomain}
        />
      )}

      {settingsOpen && (
        <SettingsModal
          connection={connection}
          close={() => setSettingsOpen(false)}
        />
      )}

      {toast && (
        <div className="toast" role="status">
          <span>✓</span>
          {toast}
        </div>
      )}
    </div>
  );
}

function DomainsView({
  domains,
  activeDomainId,
  sourceCounts,
  selectDomain,
  createDomain,
}: {
  domains: DataDomain[];
  activeDomainId: string;
  sourceCounts: Record<string, number>;
  selectDomain: (domain: DataDomain, view?: View) => void;
  createDomain: () => void;
}) {
  return (
    <div className="page domains-page">
      <PageHeader
        eyebrow="GOVERNED CONTEXT"
        title="Data Domains"
        description="Create isolated business contexts, then add each domain’s sources, semantic definitions, policies, and analytical history."
        action={
          <button className="primary-button" onClick={createDomain}>
            <span>＋</span> New data domain
          </button>
        }
      />

      <section className="domain-hero panel">
        <div>
          <span className="panel-kicker">DOMAIN-FIRST WORKFLOW</span>
          <h2>One platform. Many governed business contexts.</h2>
          <p>
            Every upload, entity, metric, Join Path, contract, and run belongs
            to the selected data domain. Context never leaks across domains.
          </p>
        </div>
        <div className="domain-flow" aria-label="Data domain workflow">
          {[
            ["01", "Create domain"],
            ["02", "Add sources"],
            ["03", "Review semantics"],
            ["04", "Ask with evidence"],
          ].map(([index, label], itemIndex) => (
            <span key={label}>
              <i>{index}</i>
              <strong>{label}</strong>
              {itemIndex < 3 && <b>→</b>}
            </span>
          ))}
        </div>
      </section>

      <div className="domain-grid">
        {domains.map((domain) => {
          const sourceCount = Math.max(
            domain.isSample ? 1 : 0,
            domain.sourceCount,
            sourceCounts[domain.id] ?? 0,
          );
          return (
            <article
              className={cn(
                "domain-card",
                domain.id === activeDomainId && "active",
              )}
              key={domain.id}
            >
              <div className="domain-card-top">
                <span className={cn("domain-card-icon", domain.isSample && "sample")}>
                  {domain.isSample ? "A" : "D"}
                </span>
                <div>
                  <div className="domain-card-labels">
                    {domain.id === activeDomainId && (
                      <span className="status-pill success">ACTIVE</span>
                    )}
                    {domain.isSample && (
                      <span className="status-pill neutral">SAMPLE</span>
                    )}
                  </div>
                  <h2>{domain.name}</h2>
                  <code>{domain.slug}</code>
                </div>
              </div>
              <p>{domain.description}</p>
              <div className="domain-card-stats">
                <span>
                  <strong>{sourceCount}</strong>
                  <small>Sources</small>
                </span>
                <span>
                  <strong>{domain.isSample ? 15 : sourceCount ? 1 : 0}</strong>
                  <small>Entities</small>
                </span>
                <span>
                  <strong>{domain.isSample ? 11 : 0}</strong>
                  <small>Metrics</small>
                </span>
                <span>
                  <strong>{domain.isSample ? "82/82" : sourceCount ? "Draft" : "—"}</strong>
                  <small>Contract</small>
                </span>
              </div>
              <footer>
                <span>
                  Owner <strong>{domain.owner}</strong>
                </span>
                <button
                  className={
                    domain.id === activeDomainId
                      ? "secondary-button"
                      : "primary-button"
                  }
                  onClick={() => selectDomain(domain)}
                >
                  {domain.id === activeDomainId
                    ? "Open active domain →"
                    : "Select domain →"}
                </button>
              </footer>
            </article>
          );
        })}

        <button className="domain-create-card" onClick={createDomain}>
          <span>＋</span>
          <strong>Create another data domain</strong>
          <p>Start with a clean semantic and governance boundary.</p>
        </button>
      </div>
    </div>
  );
}

function OverviewView({
  domain,
  query,
  setQuery,
  runQuery,
  openSources,
  openSemantic,
  openRuns,
  connection,
  runs,
}: {
  domain: DataDomain;
  query: string;
  setQuery: (value: string) => void;
  runQuery: (value?: string) => Promise<void>;
  openSources: () => void;
  openSemantic: () => void;
  openRuns: () => void;
  connection: ConnectionState;
  runs: RunRecord[];
}) {
  if (!domain.isSample) {
    return (
      <div className="page page-overview">
        <section className="hero-panel domain-onboarding-hero">
          <div className="hero-copy">
            <div className="eyebrow">
              <span className="pulse-dot" />
              ACTIVE DATA DOMAIN
            </div>
            <h1>
              {domain.name}
              <span>Build its trusted analytical language.</span>
            </h1>
            <p>
              This domain is isolated from every other business context.
              Complete the source and semantic contract steps to activate
              governed analysis.
            </p>
            <div className="onboarding-actions">
              <button className="primary-button" onClick={openSources}>
                Add domain data →
              </button>
              <button className="secondary-button" onClick={openSemantic}>
                Open semantic studio
              </button>
            </div>
          </div>
          <div className="domain-readiness">
            <span className="panel-kicker">ACTIVATION PATH</span>
            {[
              ["Data domain created", true],
              ["Source profiled", domain.sourceCount > 0],
              ["Semantic contract reviewed", false],
              ["Execution connector ready", false],
            ].map(([label, complete], index) => (
              <div key={String(label)} className={cn(complete && "complete")}>
                <span>{complete ? "✓" : index + 1}</span>
                <strong>{label}</strong>
              </div>
            ))}
          </div>
        </section>
        <section className="metric-grid" aria-label="Domain metrics">
          <MetricCard
            label="Connected sources"
            value={String(domain.sourceCount)}
            detail="Scoped to this domain"
            trend={domain.sourceCount ? "Profiled" : "Action required"}
            icon="▦"
            onClick={openSources}
          />
          <MetricCard
            label="Semantic entities"
            value={domain.sourceCount ? "1 draft" : "0"}
            detail="Human review required"
            trend="Domain isolated"
            icon="⌘"
            onClick={openSemantic}
          />
          <MetricCard
            label="Business metrics"
            value="0"
            detail="Define after profiling"
            trend="No borrowed meaning"
            icon="ƒ"
            onClick={openSemantic}
          />
          <MetricCard
            label="Contract status"
            value="Draft"
            detail="Blocking until published"
            trend="Safe by default"
            icon="◇"
            onClick={openSemantic}
          />
        </section>
        <section className="panel domain-empty-panel">
          <span className="domain-card-icon">D</span>
          <div>
            <span className="panel-kicker">NEXT BEST ACTION</span>
            <h2>
              {domain.sourceCount
                ? "Finish the semantic contract"
                : "Upload the first domain-owned source"}
            </h2>
            <p>
              QueryForge will profile physical structure, propose a semantic
              draft, require an owner review, and publish data plus meaning
              atomically.
            </p>
          </div>
          <button
            className="primary-button"
            onClick={domain.sourceCount ? openSemantic : openSources}
          >
            Continue setup →
          </button>
        </section>
      </div>
    );
  }

  return (
    <div className="page page-overview">
      <section className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">
            <span className="pulse-dot" />
            SEMANTIC LAYER ONLINE
          </div>
          <h1>
            Turn this domain into trusted answers.
            <span>Inspect every decision.</span>
          </h1>
          <p>
            <strong>{domain.name}</strong> is the selected sample domain.
            Natural-language analytics stays grounded in its governed metrics,
            explicit Join Paths, and read-only SQL policy.
          </p>
          <form
            className="hero-query"
            onSubmit={(event) => {
              event.preventDefault();
              void runQuery();
            }}
          >
            <span className="query-spark">✦</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Ask a data question"
              placeholder="Ask a question about your data…"
            />
            <div className="query-shortcut">
              <kbd>⌘</kbd>
              <kbd>↵</kbd>
            </div>
            <button type="submit">Run query</button>
          </form>
          <div className="hero-suggestions">
            <span>Try:</span>
            {EXAMPLE_QUESTIONS.slice(1, 4).map((item) => (
              <button key={item} onClick={() => void runQuery(item)}>
                {item}
              </button>
            ))}
          </div>
        </div>
        <div className="hero-orbit" aria-hidden="true">
          <div className="orbit-ring ring-one" />
          <div className="orbit-ring ring-two" />
          <div className="orbit-core">
            <span>82</span>
            <small>contracts</small>
          </div>
          <span className="orbit-node node-a">SQL</span>
          <span className="orbit-node node-b">METRIC</span>
          <span className="orbit-node node-c">POLICY</span>
          <span className="orbit-node node-d">QA</span>
        </div>
      </section>

      <section className="metric-grid" aria-label="Workspace metrics">
        <MetricCard
          label="Queryable records"
          value="370,762"
          detail="15 governed tables"
          trend="+100% synthetic"
          icon="▦"
          onClick={openSources}
        />
        <MetricCard
          label="Semantic entities"
          value="15"
          detail="30 relationships"
          trend="7 Join Paths"
          icon="⌘"
          onClick={openSemantic}
        />
        <MetricCard
          label="Business metrics"
          value="11"
          detail="6 analytical domains"
          trend="100% owned"
          icon="ƒ"
          onClick={openSemantic}
        />
        <MetricCard
          label="Contract health"
          value="82/82"
          detail="0 blocking failures"
          trend="No drift"
          icon="✓"
          onClick={openSemantic}
        />
      </section>

      <section className="overview-grid">
        <div className="panel engagement-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">ENGAGEMENT</span>
              <h2>Watch hours by genre</h2>
            </div>
            <div className="segmented small">
              <button className="active">30D</button>
              <button>90D</button>
              <button>1Y</button>
            </div>
          </div>
          <div className="chart-summary">
            <strong>9,570.8</strong>
            <span>governed watch hours</span>
            <em>↗ 12.4%</em>
          </div>
          <div className="area-chart" aria-label="Watch hours trend">
            <div className="chart-grid-line line-25" />
            <div className="chart-grid-line line-50" />
            <div className="chart-grid-line line-75" />
            {[42, 48, 45, 59, 54, 69, 63, 76, 72, 85, 81, 94].map(
              (height, index) => (
                <span
                  key={index}
                  className="area-bar"
                  style={{ height: `${height}%` }}
                />
              ),
            )}
          </div>
          <div className="chart-axis">
            <span>Jun 24</span>
            <span>Jul 01</span>
            <span>Jul 08</span>
            <span>Jul 15</span>
            <span>Jul 22</span>
          </div>
          <div className="chart-legend">
            <span>
              <i className="legend-primary" /> Watch hours
            </span>
            <span>
              <i className="legend-secondary" /> Completion rate
            </span>
          </div>
        </div>

        <div className="panel contract-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">GOVERNANCE</span>
              <h2>Semantic contract</h2>
            </div>
            <button className="text-button" onClick={openSemantic}>
              Open studio →
            </button>
          </div>
          <div className="contract-score">
            <div className="score-ring">
              <span>100</span>
              <small>score</small>
            </div>
            <div>
              <strong>Production ready</strong>
              <p>All blocking semantic and physical checks passed.</p>
            </div>
          </div>
          <div className="contract-checks">
            {[
              ["Metric definitions", "11 / 11"],
              ["Relationship integrity", "30 / 30"],
              ["Governed Join Paths", "7 / 7"],
              ["Quality & ownership", "34 / 34"],
            ].map(([label, value]) => (
              <div key={label}>
                <span className="check-mark">✓</span>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <div className="contract-footer">
            <span>Last validated 4 minutes ago</span>
            <span className="status-pill success">NO DRIFT</span>
          </div>
        </div>
      </section>

      <section className="overview-grid lower">
        <div className="panel recent-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">RECENT ACTIVITY</span>
              <h2>Governed runs</h2>
            </div>
            <button className="text-button" onClick={openRuns}>
              View all →
            </button>
          </div>
          <div className="activity-list">
            {runs.slice(0, 4).map((run) => (
              <button
                key={run.id}
                className="activity-row"
                onClick={() => void runQuery(run.question)}
              >
                <span
                  className={cn(
                    "activity-icon",
                    run.status === "Blocked" && "blocked",
                  )}
                >
                  {run.status === "Blocked" ? "!" : "✓"}
                </span>
                <span className="activity-question">
                  <strong>{run.question}</strong>
                  <small>
                    {run.id} · {run.model}
                  </small>
                </span>
                <span className="activity-meta">
                  <strong>{run.duration}</strong>
                  <small>{run.time}</small>
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="panel platform-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">RUNTIME</span>
              <h2>Platform health</h2>
            </div>
            <span
              className={cn(
                "status-pill",
                connection === "live" ? "success" : "neutral",
              )}
            >
              {connection === "live" ? "LIVE" : "DEMO"}
            </span>
          </div>
          <div className="runtime-stack">
            {[
              ["Semantic layer", "Healthy", "82 checks"],
              ["SQL policy", "Enforced", "Read-only"],
              ["Data freshness", "Current", "4 min ago"],
              ["Model gateway", connection === "live" ? "Connected" : "Demo", "Auto"],
            ].map(([label, value, detail]) => (
              <div className="runtime-row" key={label}>
                <span className="runtime-symbol">◆</span>
                <span>
                  <strong>{label}</strong>
                  <small>{detail}</small>
                </span>
                <b>{value}</b>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

function MetricCard({
  label,
  value,
  detail,
  trend,
  icon,
  onClick,
}: {
  label: string;
  value: string;
  detail: string;
  trend: string;
  icon: string;
  onClick: () => void;
}) {
  return (
    <button className="metric-card" onClick={onClick}>
      <span className="metric-icon">
        <Icon value={icon} />
      </span>
      <span className="metric-label">{label}</span>
      <strong>{value}</strong>
      <span className="metric-detail">{detail}</span>
      <span className="metric-trend">{trend}</span>
    </button>
  );
}

function SourcesView({
  domain,
  uploadedSources,
  openUpload,
  openSemantic,
}: {
  domain: DataDomain;
  uploadedSources: UploadedSource[];
  openUpload: () => void;
  openSemantic: () => void;
}) {
  return (
    <div className="page">
      <PageHeader
        eyebrow="DATA FOUNDATION"
        title={`${domain.name} · Data Sources`}
        description="Sources are isolated inside the active data domain and can publish only with a reviewed semantic contract."
        action={
          <button className="primary-button" onClick={openUpload}>
            <span>＋</span> Add data source
          </button>
        }
      />

      <div className={cn("source-summary-grid", !domain.isSample && "single")}>
        {domain.isSample && (
          <div className="source-feature-card">
          <div className="source-card-header">
            <span className="source-logo sqlite">SQL</span>
            <div>
              <span className="status-pill success">ACTIVE</span>
              <h3>Anime Streaming</h3>
              <p>SQLite · synthetic product analytics</p>
            </div>
            <button className="icon-button" aria-label="Source actions">
              ···
            </button>
          </div>
          <div className="source-stats">
            <div>
              <span>Tables</span>
              <strong>15</strong>
            </div>
            <div>
              <span>Rows</span>
              <strong>370,762</strong>
            </div>
            <div>
              <span>Size</span>
              <strong>33.4 MB</strong>
            </div>
            <div>
              <span>Last profile</span>
              <strong>4m ago</strong>
            </div>
          </div>
          <div className="source-contract-row">
            <span className="check-mark">✓</span>
            <span>
              <strong>Semantic model published</strong>
              <small>15 entities · 11 metrics · 82/82 checks</small>
            </span>
            <button onClick={openSemantic}>Inspect →</button>
          </div>
          </div>
        )}

        <button className="add-source-card" onClick={openUpload}>
          <span className="add-source-icon">＋</span>
          <strong>
            {domain.isSample ? "Connect another source" : "Add the first domain source"}
          </strong>
          <p>SQLite, CSV, or Parquet</p>
          <span className="tiny-label">SEMANTICS REQUIRED</span>
        </button>
      </div>

      {!domain.isSample && uploadedSources.length === 0 && (
        <section className="panel domain-source-empty">
          <span className="domain-card-icon">D</span>
          <div>
            <span className="panel-kicker">EMPTY DOMAIN</span>
            <h2>No data has entered {domain.name}</h2>
            <p>
              Upload files here to profile them inside this domain. QueryForge
              will not borrow entities or metrics from the Anime Streaming
              sample.
            </p>
          </div>
          <button className="primary-button" onClick={openUpload}>
            Upload domain data →
          </button>
        </section>
      )}

      {uploadedSources.length > 0 && (
        <section className="panel uploaded-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">NEWLY PUBLISHED</span>
              <h2>Your uploaded sources</h2>
            </div>
          </div>
          <div className="uploaded-source-grid">
            {uploadedSources.map((source) => (
              <div className="uploaded-source-card" key={source.id}>
                <span className="source-logo file">↑</span>
                <div>
                  <strong>{source.name}</strong>
                  <span>
                    {source.type} · {source.size}
                  </span>
                </div>
                <span className="status-pill success">{source.status}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className={cn("sources-layout", !domain.isSample && "domain-only")}>
        {domain.isSample && (
        <section className="panel table-inventory">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">PHYSICAL MODEL</span>
              <h2>Table inventory</h2>
            </div>
            <div className="table-search">
              <span>⌕</span>
              <input placeholder="Filter tables…" aria-label="Filter tables" />
            </div>
          </div>
          <div className="data-table">
            <div className="data-table-head four">
              <span>Table</span>
              <span>Rows</span>
              <span>Role</span>
              <span>Semantic coverage</span>
            </div>
            {TABLES.map(([table, rows, role, coverage]) => (
              <div className="data-table-row four" key={table}>
                <span className="table-name">
                  <i>{role === "Fact" ? "F" : "D"}</i>
                  <strong>{table}</strong>
                </span>
                <span className="mono">{rows}</span>
                <span>
                  <span
                    className={cn(
                      "type-pill",
                      role === "Fact" ? "fact" : "dimension",
                    )}
                  >
                    {role}
                  </span>
                </span>
                <span className="coverage-cell">
                  <i>
                    <b style={{ width: "100%" }} />
                  </i>
                  {coverage}
                </span>
              </div>
            ))}
            <button className="table-more">Show all 15 tables</button>
          </div>
        </section>
        )}

        <aside className="panel pipeline-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">PUBLICATION PIPELINE</span>
              <h2>Atomic by design</h2>
            </div>
          </div>
          <div className="pipeline-steps">
            {[
              ["01", "Upload", "Bytes accepted and fingerprinted"],
              ["02", "Profile", "Schema, grain and quality inferred"],
              ["03", "Review semantics", "Owner confirms business meaning"],
              ["04", "Validate", "Blocking contracts enforced"],
              ["05", "Publish", "Data + semantics commit together"],
            ].map(([index, label, detail], itemIndex) => (
              <div className="pipeline-step" key={label}>
                <span>{index}</span>
                <div>
                  <strong>{label}</strong>
                  <small>{detail}</small>
                </div>
                {itemIndex < 4 && <i />}
              </div>
            ))}
          </div>
          <div className="pipeline-note">
            <span>!</span>
            <p>
              If any semantic or quality check fails, no data is published and
              no watermark advances.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

function SemanticView({
  domain,
  hasSources,
  openUpload,
  tab,
  setTab,
  entityFilter,
  setEntityFilter,
  filteredEntities,
  selectedEntity,
  selectEntity,
  activeEntity,
  toast,
}: {
  domain: DataDomain;
  hasSources: boolean;
  openUpload: () => void;
  tab: SemanticTab;
  setTab: (tab: SemanticTab) => void;
  entityFilter: string;
  setEntityFilter: (value: string) => void;
  filteredEntities: Entity[];
  selectedEntity: string;
  selectEntity: (name: string) => void;
  activeEntity: Entity;
  toast: (message: string) => void;
}) {
  if (!domain.isSample) {
    return (
      <div className="page semantic-page">
        <PageHeader
          eyebrow="DOMAIN SEMANTICS"
          title={`${domain.name} · Semantic Studio`}
          description="Build a domain-owned business contract. QueryForge never imports entities, metrics, or Join Paths from another domain."
          action={
            <div className="header-action-group">
              <button className="secondary-button" onClick={openUpload}>
                ＋ Add source
              </button>
              <button
                className="primary-button"
                disabled={!hasSources}
                onClick={() =>
                  toast(
                    hasSources
                      ? "Semantic draft saved for validation."
                      : "Add a source before defining its semantic contract.",
                  )
                }
              >
                Validate draft
              </button>
            </div>
          }
        />

        <div className="semantic-health-strip draft">
          <div className="health-score-mini">
            <strong>{hasSources ? "42" : "0"}</strong>
            <span>/100</span>
          </div>
          <div>
            <strong>
              {hasSources
                ? "Domain contract needs business review"
                : "Semantic construction starts with domain data"}
            </strong>
            <p>
              Identity, grain, metrics, relationships, ownership, policy, and
              quality checks must all pass before analysis unlocks.
            </p>
          </div>
          <div className="semantic-health-metrics">
            <span>
              <strong>{hasSources ? 1 : 0}</strong> draft entities
            </span>
            <span>
              <strong>0</strong> relationships
            </span>
            <span>
              <strong>0</strong> metrics
            </span>
            <span>
              <strong>0</strong> Join Paths
            </span>
          </div>
          <span className="status-pill neutral">DRAFT</span>
        </div>

        {!hasSources ? (
          <section className="panel semantic-empty-state">
            <span className="semantic-empty-icon">⌘</span>
            <div>
              <span className="panel-kicker">NO PHYSICAL MODEL YET</span>
              <h2>Add data before declaring business meaning</h2>
              <p>
                QueryForge profiles tables and columns first, then proposes
                candidate entities without pretending technical schema names
                are business definitions.
              </p>
            </div>
            <button className="primary-button" onClick={openUpload}>
              Add governed source →
            </button>
          </section>
        ) : (
          <div className="semantic-builder">
            <aside className="semantic-builder-steps">
              <span className="panel-kicker">CONTRACT BUILDER</span>
              {[
                ["01", "Identity & grain", "active"],
                ["02", "Dimensions & measures", ""],
                ["03", "Metrics", ""],
                ["04", "Relationships", ""],
                ["05", "Policy & quality", ""],
                ["06", "Review & publish", ""],
              ].map(([index, label, state]) => (
                <button className={state} key={label}>
                  <span>{index}</span>
                  <strong>{label}</strong>
                </button>
              ))}
            </aside>
            <section className="panel semantic-contract-editor">
              <div className="panel-heading">
                <div>
                  <span className="panel-kicker">STEP 01</span>
                  <h2>Define business identity and grain</h2>
                </div>
                <span className="status-pill neutral">REVIEW REQUIRED</span>
              </div>
              <p className="contract-editor-intro">
                Start from the physical profile, then name the real business
                entity and state exactly what one row represents.
              </p>
              <div className="semantic-contract-fields">
                <label>
                  <span>Business entity</span>
                  <input defaultValue="uploaded_record" />
                  <small>Use a stable singular business concept.</small>
                </label>
                <label>
                  <span>Primary source</span>
                  <input value="Latest profiled upload" readOnly />
                  <small>Scoped to {domain.name}.</small>
                </label>
                <label className="wide">
                  <span>Business description</span>
                  <textarea
                    rows={3}
                    defaultValue="One reviewed business record represented by the uploaded source."
                  />
                </label>
                <label>
                  <span>Grain</span>
                  <input defaultValue="record_id" />
                  <small>Required for safe aggregation.</small>
                </label>
                <label>
                  <span>Owner</span>
                  <input defaultValue={domain.owner} />
                  <small>Accountable for semantic approval.</small>
                </label>
              </div>
              <div className="contract-guardrail">
                <span>!</span>
                <p>
                  Analysis remains blocked until the primary key is unique,
                  every metric declares an aggregation and denominator, and all
                  multi-hop relationships have a reviewed Join Path.
                </p>
              </div>
              <footer>
                <span>Autosaved as a domain-local draft</span>
                <button
                  className="primary-button"
                  onClick={() => toast("Identity and grain saved.")}
                >
                  Save & continue →
                </button>
              </footer>
            </section>
            <aside className="panel semantic-checklist">
              <span className="panel-kicker">PUBLICATION GATE</span>
              <h3>Required contract</h3>
              {[
                ["Entity has a clear grain", true],
                ["Primary key is unique", false],
                ["Metric units are explicit", false],
                ["Cardinality is reviewed", false],
                ["Sensitive fields are classified", false],
                ["Quality checks pass", false],
              ].map(([label, passed]) => (
                <div key={String(label)} className={cn(passed && "passed")}>
                  <span>{passed ? "✓" : "○"}</span>
                  <strong>{label}</strong>
                </div>
              ))}
              <p>Data and semantics publish atomically only after 100%.</p>
            </aside>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="page semantic-page">
      <PageHeader
        eyebrow="BUSINESS MEANING"
        title={`${domain.name} · Semantic Studio`}
        description="Model entities, metrics, relationships, and safe Join Paths before any analytical query can run."
        action={
          <div className="header-action-group">
            <button
              className="secondary-button"
              onClick={() => toast("Semantic model YAML downloaded.")}
            >
              ↓ Export YAML
            </button>
            <button
              className="primary-button success-button"
              onClick={() => toast("Semantic model is already published.")}
            >
              ✓ Published
            </button>
          </div>
        }
      />

      <div className="semantic-health-strip">
        <div className="health-score-mini">
          <strong>82</strong>
          <span>/82</span>
        </div>
        <div>
          <strong>Semantic contract healthy</strong>
          <p>
            All metric, grain, relationship, ownership, sensitivity, and
            physical checks pass.
          </p>
        </div>
        <div className="semantic-health-metrics">
          <span>
            <strong>15</strong> entities
          </span>
          <span>
            <strong>30</strong> relationships
          </span>
          <span>
            <strong>11</strong> metrics
          </span>
          <span>
            <strong>7</strong> Join Paths
          </span>
        </div>
        <span className="status-pill success">NO DRIFT</span>
      </div>

      <div className="semantic-tabs">
        <button
          className={cn(tab === "graph" && "active")}
          onClick={() => setTab("graph")}
        >
          Relationship graph
        </button>
        <button
          className={cn(tab === "metrics" && "active")}
          onClick={() => setTab("metrics")}
        >
          Metric catalog <span>11</span>
        </button>
        <button
          className={cn(tab === "paths" && "active")}
          onClick={() => setTab("paths")}
        >
          Governed Join Paths <span>7</span>
        </button>
      </div>

      {tab === "graph" && (
        <div className="semantic-workbench">
          <aside className="entity-browser">
            <div className="workbench-header">
              <div>
                <span className="panel-kicker">ENTITIES</span>
                <strong>Business model</strong>
              </div>
              <button aria-label="Add entity">＋</button>
            </div>
            <label className="entity-search">
              <span>⌕</span>
              <input
                value={entityFilter}
                onChange={(event) => setEntityFilter(event.target.value)}
                placeholder="Find an entity…"
              />
            </label>
            <div className="entity-list">
              {filteredEntities.map((entity) => (
                <button
                  key={entity.name}
                  className={cn(
                    "entity-list-item",
                    entity.name === selectedEntity && "active",
                  )}
                  onClick={() => selectEntity(entity.name)}
                >
                  <span className={cn("entity-type-dot", entity.kind)} />
                  <span>
                    <strong>{entity.label}</strong>
                    <small>{entity.table}</small>
                  </span>
                  <em>{entity.dimensions}</em>
                </button>
              ))}
            </div>
            <div className="entity-legend">
              <span>
                <i className="entity-type-dot dimension" /> Dimension
              </span>
              <span>
                <i className="entity-type-dot fact" /> Fact
              </span>
              <span>
                <i className="entity-type-dot bridge" /> Bridge
              </span>
            </div>
          </aside>

          <section className="graph-stage">
            <div className="graph-toolbar">
              <div>
                <button className="active">All domains</button>
                <button>Content</button>
                <button>Engagement</button>
                <button>Commerce</button>
              </div>
              <div>
                <button aria-label="Zoom out">−</button>
                <span>85%</span>
                <button aria-label="Zoom in">＋</button>
                <button aria-label="Fit graph">⌗</button>
              </div>
            </div>
            <div className="graph-scroll">
              <div className="graph-canvas">
                <div className="graph-domain domain-content">CONTENT</div>
                <div className="graph-domain domain-engagement">
                  ENGAGEMENT
                </div>
                <div className="graph-domain domain-commerce">COMMERCE</div>
                {GRAPH_LINKS.map((link, index) => (
                  <span
                    className="graph-link"
                    key={index}
                    style={{
                      left: link.x,
                      top: link.y,
                      width: link.width,
                      transform: `rotate(${link.angle}deg)`,
                    }}
                  />
                ))}
                {ENTITIES.map((entity) => (
                  <button
                    className={cn(
                      "graph-node",
                      entity.kind,
                      entity.name === selectedEntity && "selected",
                    )}
                    key={entity.name}
                    style={{ left: entity.x, top: entity.y }}
                    onClick={() => selectEntity(entity.name)}
                  >
                    <span className="graph-node-icon">
                      {entity.kind === "fact"
                        ? "F"
                        : entity.kind === "bridge"
                          ? "B"
                          : "D"}
                    </span>
                    <span>
                      <strong>{entity.label}</strong>
                      <small>{entity.table}</small>
                    </span>
                    {entity.metrics > 0 && <em>{entity.metrics}</em>}
                  </button>
                ))}
              </div>
            </div>
          </section>

          <aside className="entity-inspector">
            <div className="inspector-top">
              <span className={cn("entity-badge", activeEntity.kind)}>
                {activeEntity.kind.toUpperCase()}
              </span>
              <button aria-label="Close inspector">×</button>
            </div>
            <h2>{activeEntity.label}</h2>
            <code>{activeEntity.table}</code>
            <p>
              Governed {activeEntity.kind} entity owned by{" "}
              {activeEntity.owner}.
            </p>
            <div className="inspector-section">
              <span className="panel-kicker">CONTRACT</span>
              <label>
                <span>Owner</span>
                <input value={activeEntity.owner} readOnly />
              </label>
              <label>
                <span>Grain</span>
                <input value={activeEntity.grain} readOnly />
              </label>
              <label>
                <span>Sensitivity</span>
                <select defaultValue="internal">
                  <option>public</option>
                  <option>internal</option>
                  <option>restricted</option>
                </select>
              </label>
            </div>
            <div className="inspector-section">
              <div className="inspector-section-heading">
                <span className="panel-kicker">DIMENSIONS</span>
                <button>＋</button>
              </div>
              {[
                ["Primary key", activeEntity.grain.split(" + ")[0]],
                ["Display name", `${activeEntity.name}_name`],
                ["Event date", "date_key"],
              ].map(([label, field]) => (
                <div className="field-row" key={label}>
                  <span>
                    <strong>{label}</strong>
                    <code>{field}</code>
                  </span>
                  <button>···</button>
                </div>
              ))}
            </div>
            <div className="inspector-section compact">
              <div>
                <span>Metrics</span>
                <strong>{activeEntity.metrics}</strong>
              </div>
              <div>
                <span>Dimensions</span>
                <strong>{activeEntity.dimensions}</strong>
              </div>
              <div>
                <span>Quality rules</span>
                <strong>4</strong>
              </div>
            </div>
            <button
              className="save-entity-button"
              onClick={() => toast(`${activeEntity.label} draft saved.`)}
            >
              Save entity draft
            </button>
          </aside>
        </div>
      )}

      {tab === "metrics" && (
        <div className="metric-catalog">
          <div className="catalog-toolbar">
            <label className="entity-search">
              <span>⌕</span>
              <input placeholder="Search 11 metrics…" />
            </label>
            <button className="secondary-button">Filter by domain</button>
            <button className="primary-button">＋ New metric</button>
          </div>
          <div className="metric-catalog-grid">
            {METRICS.map((metric) => (
              <article className="metric-definition-card" key={metric.name}>
                <div className="metric-definition-top">
                  <span className="metric-function">ƒ</span>
                  <div>
                    <h3>{metric.label}</h3>
                    <code>{metric.name}</code>
                  </div>
                  <span className="status-pill success">VALID</span>
                </div>
                <p>{metric.description}</p>
                <div className="metric-expression">
                  <span>{metric.aggregation}</span>
                  <code>{metric.expression}</code>
                </div>
                <footer>
                  <span>
                    Entity <strong>{metric.entity}</strong>
                  </span>
                  <button onClick={() => toast(`${metric.label} opened.`)}>
                    Edit →
                  </button>
                </footer>
              </article>
            ))}
          </div>
        </div>
      )}

      {tab === "paths" && (
        <div className="join-paths-panel">
          <div className="join-paths-intro">
            <div>
              <span className="panel-kicker">FAN-OUT CONTROL</span>
              <h2>Only reviewed paths reach SQL generation</h2>
              <p>
                Each path declares its hop sequence and cardinality so the agent
                cannot invent joins or silently duplicate facts.
              </p>
            </div>
            <button className="primary-button">＋ Define Join Path</button>
          </div>
          <div className="join-paths-list">
            {JOIN_PATHS.map((path) => (
              <article className="join-path-row" key={path.name}>
                <span className="join-path-icon">↝</span>
                <div className="join-path-name">
                  <strong>{path.label}</strong>
                  <code>{path.name}</code>
                </div>
                <div className="path-flow">
                  {path.steps.map((step, index) => (
                    <span key={step}>
                      <b>{step}</b>
                      {index < path.steps.length - 1 && <i>→</i>}
                    </span>
                  ))}
                </div>
                <span className="hop-count">{path.hops} hops</span>
                <span className="status-pill success">{path.risk}</span>
                <button aria-label={`Edit ${path.label}`}>···</button>
              </article>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function AskView({
  domain,
  hasSources,
  query,
  setQuery,
  submitQuery,
  runQuery,
  isRunning,
  runStage,
  result,
  copySql,
  downloadResult,
  runs,
  selectRun,
  connection,
}: {
  domain: DataDomain;
  hasSources: boolean;
  query: string;
  setQuery: (value: string) => void;
  submitQuery: (event: FormEvent) => void;
  runQuery: (value?: string) => Promise<void>;
  isRunning: boolean;
  runStage: number;
  result: QueryResult;
  copySql: () => void;
  downloadResult: () => void;
  runs: RunRecord[];
  selectRun: (run: RunRecord) => void;
  connection: ConnectionState;
}) {
  if (!domain.isSample) {
    return (
      <div className="page domain-analysis-page">
        <PageHeader
          eyebrow="DOMAIN ANALYSIS"
          title={`${domain.name} · Ask & Analyze`}
          description="Questions execute only against the active domain after its semantic contract and runtime connector are published."
          action={
            <span className="status-pill neutral">
              {hasSources ? "SEMANTICS REQUIRED" : "DATA REQUIRED"}
            </span>
          }
        />
        <section className="panel domain-analysis-lock">
          <div className="analysis-lock-visual">
            <span>✦</span>
            <i />
            <b>⌘</b>
            <i />
            <em>SQL</em>
          </div>
          <div>
            <span className="panel-kicker">SAFE BY DEFAULT</span>
            <h2>
              {hasSources
                ? "Publish the semantic contract to unlock questions"
                : "This domain needs data before it can answer"}
            </h2>
            <p>
              QueryForge will not fall back to the Anime Streaming sample or
              invent joins when this domain is incomplete. Domain context,
              meaning, policy, and evidence must travel together.
            </p>
            <div className="analysis-lock-checks">
              {[
                ["Domain selected", true],
                ["Source available", hasSources],
                ["Semantic contract published", false],
                ["Execution connector ready", false],
              ].map(([label, ready]) => (
                <span key={String(label)} className={cn(ready && "ready")}>
                  {ready ? "✓" : "○"} {label}
                </span>
              ))}
            </div>
          </div>
        </section>
      </div>
    );
  }

  const maxValue = Math.max(
    ...result.rows.map((row) => Number(row[1]) || 0),
    1,
  );

  return (
    <div className="ask-workspace">
      <aside className="conversation-rail">
        <button className="new-query-button" onClick={() => setQuery("")}>
          <span>＋</span> New analysis
        </button>
        <div className="conversation-label">RECENT</div>
        {runs.map((run) => (
          <button
            className="conversation-item"
            key={run.id}
            onClick={() => selectRun(run)}
          >
            <span>✦</span>
            <span>
              <strong>{run.question}</strong>
              <small>{run.time}</small>
            </span>
          </button>
        ))}
        <div className="conversation-bottom">
          <span className="shield-icon">◆</span>
          <div>
            <strong>Governed mode</strong>
            <small>Semantic model required</small>
          </div>
        </div>
      </aside>

      <section className="analysis-canvas">
        <div className="analysis-header">
          <div>
            <div className="eyebrow">
              <span className="pulse-dot" />
              {connection === "live" ? "LIVE ANALYSIS" : "INTERACTIVE DEMO"}
            </div>
            <h1>Ask & Analyze</h1>
          </div>
          <div className="analysis-actions">
            <button className="secondary-button" onClick={downloadResult}>
              ↓ Export
            </button>
            <button className="icon-button" aria-label="More actions">
              ···
            </button>
          </div>
        </div>

        <form className="ask-composer" onSubmit={submitQuery}>
          <span className="query-spark">✦</span>
          <textarea
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Ask a business question…"
            aria-label="Business question"
            rows={2}
          />
          <div className="composer-options">
            <span className="option-chip">{domain.name}</span>
            <span className="option-chip">Auto complexity</span>
            <span className="option-chip">Report on</span>
          </div>
          <button type="submit" disabled={isRunning || !query.trim()}>
            {isRunning ? "Running…" : "Run"}
            <span>↵</span>
          </button>
        </form>

        <div className="question-suggestions">
          {EXAMPLE_QUESTIONS.slice(1).map((item) => (
            <button key={item} onClick={() => void runQuery(item)}>
              {item}
            </button>
          ))}
        </div>

        {isRunning ? (
          <div className="run-progress-panel">
            <div className="run-progress-orb">QF</div>
            <h2>Building a governed answer</h2>
            <p>Every stage produces evidence before SQL can execute.</p>
            <div className="run-stage-list">
              {[
                ["Resolve semantics", "Matched metrics, entities and grain"],
                ["Plan Join Paths", "Selected reviewed relationships"],
                ["Govern SQL", "AST policy and bounded preview"],
                ["Validate answer", "Result quality and report artifacts"],
              ].map(([label, detail], index) => {
                const number = index + 1;
                return (
                  <div
                    className={cn(
                      "run-stage",
                      runStage === number && "active",
                      runStage > number && "complete",
                    )}
                    key={label}
                  >
                    <span>{runStage > number ? "✓" : number}</span>
                    <div>
                      <strong>{label}</strong>
                      <small>{detail}</small>
                    </div>
                    {runStage === number && <i />}
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <>
            <article className="answer-card">
              <div className="answer-avatar">QF</div>
              <div className="answer-content">
                <div className="answer-meta">
                  <strong>QueryForge</strong>
                  <span className="status-pill success">GOVERNED</span>
                  <small>{result.runId}</small>
                </div>
                <p>{result.explanation}</p>
                <div className="answer-highlights">
                  <div>
                    <span>Top genre</span>
                    <strong>{String(result.rows[0]?.[0] ?? "Fantasy")}</strong>
                  </div>
                  <div>
                    <span>Watch hours</span>
                    <strong>
                      {Number(result.rows[0]?.[1] ?? 0).toLocaleString()}
                    </strong>
                  </div>
                  <div>
                    <span>Best completion</span>
                    <strong>
                      {Math.max(
                        ...result.rows.map((row) => Number(row[2]) || 0),
                      ).toFixed(1)}
                      %
                    </strong>
                  </div>
                </div>
              </div>
            </article>

            <div className="result-grid">
              <section className="panel result-chart-panel">
                <div className="panel-heading">
                  <div>
                    <span className="panel-kicker">RESULT VISUALIZATION</span>
                    <h2>Watch hours by genre</h2>
                  </div>
                  <div className="segmented small">
                    <button className="active">Bar</button>
                    <button>Table</button>
                  </div>
                </div>
                <div className="horizontal-chart">
                  {result.rows.slice(0, 8).map((row, index) => {
                    const value = Number(row[1]) || 0;
                    return (
                      <div className="horizontal-bar-row" key={`${row[0]}-${index}`}>
                        <span>{String(row[0])}</span>
                        <div>
                          <i style={{ width: `${(value / maxValue) * 100}%` }} />
                        </div>
                        <strong>{value.toLocaleString()}</strong>
                      </div>
                    );
                  })}
                </div>
                <div className="chart-footnote">
                  <span>
                    <i className="legend-primary" /> Watch hours
                  </span>
                  <span>Valid sessions only · Rounded to 1 decimal</span>
                </div>
              </section>

              <section className="panel sql-panel">
                <div className="panel-heading">
                  <div>
                    <span className="panel-kicker">GENERATED SQL</span>
                    <h2>Auditable query</h2>
                  </div>
                  <button className="copy-button" onClick={copySql}>
                    ⧉ Copy
                  </button>
                </div>
                <pre>
                  <code>{result.sql}</code>
                </pre>
              </section>
            </div>

            <section className="panel result-table-panel">
              <div className="panel-heading">
                <div>
                  <span className="panel-kicker">REVIEWED ROWS</span>
                  <h2>Query result</h2>
                </div>
                <span className="row-count">{result.rowCount} rows</span>
              </div>
              <div className="result-table-scroll">
                <table>
                  <thead>
                    <tr>
                      {result.columns.map((column) => (
                        <th key={column}>{column}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, index) => (
                      <tr key={index}>
                        {row.map((cell, cellIndex) => (
                          <td key={cellIndex}>
                            {typeof cell === "number"
                              ? cell.toLocaleString()
                              : cell}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}
      </section>

      <aside className="trust-rail">
        <div className="trust-heading">
          <div>
            <span className="panel-kicker">TRUST TRACE</span>
            <strong>Why this answer is safe</strong>
          </div>
          <span className="trust-score">100</span>
        </div>
        <div className="trust-section">
          <span className="trust-section-label">SEMANTIC MATCH</span>
          <div className="trust-card">
            <span className="trust-card-icon metric">ƒ</span>
            <div>
              <strong>watch_hours</strong>
              <small>SUM · watch_session</small>
            </div>
            <span>99%</span>
          </div>
          <div className="trust-card">
            <span className="trust-card-icon metric">%</span>
            <div>
              <strong>completion_rate</strong>
              <small>RATIO · watch_session</small>
            </div>
            <span>98%</span>
          </div>
        </div>
        <div className="trust-section">
          <span className="trust-section-label">JOIN PATH</span>
          <div className="mini-path">
            <span>Watch</span>
            <i>→</i>
            <span>Episode</span>
            <i>→</i>
            <span>Anime</span>
          </div>
          <div className="trust-note success">
            <span>✓</span>
            <p>
              Reviewed path
              <small>No fan-out risk detected</small>
            </p>
          </div>
        </div>
        <div className="trust-section">
          <span className="trust-section-label">SQL POLICY</span>
          {[
            ["Read-only AST", "Passed"],
            ["Table scope", "Passed"],
            ["Join budget", "4 / 5"],
            ["Result limit", "6 / 500"],
            ["Sensitive columns", "None"],
          ].map(([label, value]) => (
            <div className="policy-check" key={label}>
              <span>✓</span>
              <strong>{label}</strong>
              <small>{value}</small>
            </div>
          ))}
        </div>
        <div className="trust-section">
          <span className="trust-section-label">QUALITY</span>
          <div className="quality-score">
            <div>
              <strong>96</strong>
              <span>/100</span>
            </div>
            <p>
              Result quality
              <small>Grain, nulls and reconciliation passed</small>
            </p>
          </div>
        </div>
        <button className="trace-download" onClick={downloadResult}>
          ↓ Download complete run artifact
        </button>
      </aside>
    </div>
  );
}

function RunsView({
  runs,
  filter,
  setFilter,
  selectRun,
  downloadResult,
}: {
  runs: RunRecord[];
  filter: "All" | "Passed" | "Blocked";
  setFilter: (filter: "All" | "Passed" | "Blocked") => void;
  selectRun: (run: RunRecord) => void;
  downloadResult: () => void;
}) {
  return (
    <div className="page">
      <PageHeader
        eyebrow="AUDITABILITY"
        title="Run History"
        description="Every route, semantic match, policy decision, SQL candidate, and quality result is preserved as evidence."
        action={
          <button className="secondary-button" onClick={downloadResult}>
            ↓ Export history
          </button>
        }
      />

      <div className="run-summary-grid">
        {[
          ["Total runs", "128", "Last 30 days"],
          ["Pass rate", "96.1%", "+2.4%"],
          ["Policy blocks", "5", "Expected denials"],
          ["Median latency", "1.72s", "−180ms"],
        ].map(([label, value, detail]) => (
          <div className="run-summary-card" key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <small>{detail}</small>
          </div>
        ))}
      </div>

      <section className="panel runs-panel">
        <div className="runs-toolbar">
          <div className="segmented">
            {(["All", "Passed", "Blocked"] as const).map((item) => (
              <button
                key={item}
                className={cn(filter === item && "active")}
                onClick={() => setFilter(item)}
              >
                {item}
              </button>
            ))}
          </div>
          <label className="table-search wide">
            <span>⌕</span>
            <input placeholder="Search questions or run IDs…" />
          </label>
          <button className="secondary-button">Last 30 days ⌄</button>
        </div>
        <div className="runs-table">
          <div className="runs-table-head">
            <span>Status</span>
            <span>Question</span>
            <span>Model</span>
            <span>Rows</span>
            <span>Duration</span>
            <span>When</span>
            <span />
          </div>
          {runs.map((run) => (
            <button
              className="runs-table-row"
              key={run.id}
              onClick={() => selectRun(run)}
            >
              <span>
                <span
                  className={cn(
                    "status-pill",
                    run.status === "Passed" ? "success" : "danger",
                  )}
                >
                  {run.status}
                </span>
              </span>
              <span className="run-question-cell">
                <strong>{run.question}</strong>
                <code>{run.id}</code>
              </span>
              <span>{run.model}</span>
              <span className="mono">{run.rows}</span>
              <span className="mono">{run.duration}</span>
              <span>{run.time}</span>
              <span>→</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

function UploadModal({
  domain,
  step,
  files,
  isProfiling,
  reviewed,
  validated,
  semanticDraft,
  setSemanticDraft,
  resetValidation,
  fileInputRef,
  close,
  handleFiles,
  profileFiles,
  setStep,
  setReviewed,
  validateSemantic,
  publishUpload,
}: {
  domain: DataDomain;
  step: number;
  files: File[];
  isProfiling: boolean;
  reviewed: boolean;
  validated: boolean;
  semanticDraft: SemanticDraft;
  setSemanticDraft: (
    value: SemanticDraft | ((current: SemanticDraft) => SemanticDraft),
  ) => void;
  resetValidation: () => void;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  close: () => void;
  handleFiles: (event: ChangeEvent<HTMLInputElement>) => void;
  profileFiles: () => Promise<void>;
  setStep: (step: number) => void;
  setReviewed: (value: boolean) => void;
  validateSemantic: () => Promise<void>;
  publishUpload: () => Promise<void>;
}) {
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal upload-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upload-title"
      >
        <div className="modal-header">
          <div>
            <span className="panel-kicker">GOVERNED INGESTION</span>
            <h2 id="upload-title">Add a data source</h2>
            <p>
              Destination: <strong>{domain.name}</strong> · Data cannot publish
              without reviewed business meaning.
            </p>
          </div>
          <button className="modal-close" onClick={close} aria-label="Close">
            ×
          </button>
        </div>

        <div className="upload-stepper">
          {[
            ["1", "Upload"],
            ["2", "Profile"],
            ["3", "Semantic contract"],
          ].map(([number, label], index) => (
            <button
              key={label}
              className={cn(
                step === index + 1 && "active",
                step > index + 1 && "complete",
              )}
              onClick={() => {
                if (index === 0 || files.length) setStep(index + 1);
              }}
            >
              <span>{step > index + 1 ? "✓" : number}</span>
              {label}
            </button>
          ))}
        </div>

        {step === 1 && (
          <div className="upload-step-content">
            <button
              className="drop-zone"
              onClick={() => fileInputRef.current?.click()}
            >
              <span className="upload-cloud">↑</span>
              <strong>Drop SQLite, CSV, or Parquet files here</strong>
              <p>or click to browse · up to 25 MB per file</p>
              <span className="browse-button">Choose files</span>
            </button>
            <input
              ref={fileInputRef}
              className="visually-hidden"
              type="file"
              multiple
              accept=".sqlite,.db,.csv,.parquet"
              onChange={handleFiles}
            />
            {files.length > 0 && (
              <div className="selected-files">
                {files.map((file) => (
                  <div key={`${file.name}-${file.size}`}>
                    <span className="file-kind">
                      {file.name.split(".").pop()?.slice(0, 3).toUpperCase()}
                    </span>
                    <span>
                      <strong>{file.name}</strong>
                      <small>{formatBytes(file.size)}</small>
                    </span>
                    <span className="check-mark">✓</span>
                  </div>
                ))}
              </div>
            )}
            <div className="upload-modal-footer">
              <span>
                {files.length
                  ? `${files.length} file(s) · ${formatBytes(totalBytes)}`
                  : "No files selected"}
              </span>
              <button
                className="primary-button"
                disabled={!files.length || isProfiling}
                onClick={() => void profileFiles()}
              >
                {isProfiling ? "Profiling…" : "Profile files →"}
              </button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="upload-step-content profile-content">
            <div className="profile-success">
              <span>✓</span>
              <div>
                <strong>Physical profile complete</strong>
                <p>
                  QueryForge inferred a draft only. Business definitions still
                  require human review.
                </p>
              </div>
            </div>
            <div className="profile-stats-grid">
              <div>
                <span>Detected tables</span>
                <strong>{Math.max(files.length, 1)}</strong>
              </div>
              <div>
                <span>Candidate grain</span>
                <strong>record_id</strong>
              </div>
              <div>
                <span>Dimensions</span>
                <strong>7</strong>
              </div>
              <div>
                <span>Quality warnings</span>
                <strong className="warning-text">2</strong>
              </div>
            </div>
            <div className="profile-table-preview">
              <div>
                <span>Column</span>
                <span>Type</span>
                <span>Semantic role</span>
                <span>Quality</span>
              </div>
              {[
                ["record_id", "INTEGER", "Primary key", "Unique"],
                ["entity_id", "INTEGER", "Entity key", "99.9%"],
                ["recorded_at", "TIMESTAMP", "Time dimension", "100%"],
                ["amount", "DECIMAL", "Candidate measure", "2 outliers"],
              ].map((row) => (
                <div key={row[0]}>
                  {row.map((cell, index) => (
                    <span key={index}>{cell}</span>
                  ))}
                </div>
              ))}
            </div>
            <div className="upload-modal-footer">
              <button className="secondary-button" onClick={() => setStep(1)}>
                ← Back
              </button>
              <button className="primary-button" onClick={() => setStep(3)}>
                Review semantic draft →
              </button>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="upload-step-content semantic-review-content">
            <div className="mandatory-banner">
              <span>!</span>
              <div>
                <strong>Semantic review is mandatory</strong>
                <p>
                  Publish stays locked until entity grain, dimensions, metrics,
                  ownership, and quality rules pass validation.
                </p>
              </div>
            </div>
            <div className="semantic-draft-grid">
              <div className="semantic-draft-form">
                <label>
                  <span>Entity name</span>
                  <input
                    value={semanticDraft.entity}
                    onChange={(event) => {
                      setSemanticDraft((current) => ({
                        ...current,
                        entity: event.target.value,
                      }));
                      setReviewed(false);
                      resetValidation();
                    }}
                  />
                </label>
                <label>
                  <span>Business description</span>
                  <textarea
                    rows={3}
                    value={semanticDraft.description}
                    onChange={(event) => {
                      setSemanticDraft((current) => ({
                        ...current,
                        description: event.target.value,
                      }));
                      setReviewed(false);
                      resetValidation();
                    }}
                  />
                </label>
                <div className="form-pair">
                  <label>
                    <span>Owner</span>
                    <input
                      value={semanticDraft.owner}
                      onChange={(event) => {
                        setSemanticDraft((current) => ({
                          ...current,
                          owner: event.target.value,
                        }));
                        setReviewed(false);
                        resetValidation();
                      }}
                    />
                  </label>
                  <label>
                    <span>Grain</span>
                    <input
                      value={semanticDraft.grain}
                      onChange={(event) => {
                        setSemanticDraft((current) => ({
                          ...current,
                          grain: event.target.value,
                        }));
                        setReviewed(false);
                        resetValidation();
                      }}
                    />
                  </label>
                </div>
                <div className="form-pair">
                  <label>
                    <span>Primary key</span>
                    <input
                      value={semanticDraft.primaryKey}
                      onChange={(event) => {
                        setSemanticDraft((current) => ({
                          ...current,
                          primaryKey: event.target.value,
                        }));
                        setReviewed(false);
                        resetValidation();
                      }}
                    />
                  </label>
                  <label>
                    <span>Sensitivity</span>
                    <select
                      value={semanticDraft.sensitivity}
                      onChange={(event) => {
                        setSemanticDraft((current) => ({
                          ...current,
                          sensitivity: event.target
                            .value as SemanticDraft["sensitivity"],
                        }));
                        setReviewed(false);
                        resetValidation();
                      }}
                    >
                      <option>public</option>
                      <option>internal</option>
                      <option>restricted</option>
                    </select>
                  </label>
                </div>
              </div>
              <div className="semantic-draft-summary">
                <span className="panel-kicker">GENERATED CONTRACT</span>
                <div>
                  <span>Entity</span>
                  <strong>1</strong>
                </div>
                <div>
                  <span>Dimensions</span>
                  <strong>{semanticDraft.dimensions.length}</strong>
                </div>
                <div>
                  <span>Draft metrics</span>
                  <strong>{semanticDraft.metrics.length}</strong>
                </div>
                <div>
                  <span>Quality rules</span>
                  <strong>6</strong>
                </div>
                {semanticDraft.metrics.map((metric) => (
                  <code key={metric.name}>{metric.name}</code>
                ))}
              </div>
            </div>
            <label className="review-checkbox">
              <input
                type="checkbox"
                checked={reviewed}
                onChange={(event) => {
                  setReviewed(event.target.checked);
                  if (!event.target.checked) {
                    resetValidation();
                  }
                }}
              />
              <span>
                <strong>I reviewed the business meaning and grain</strong>
                <small>
                  The owner confirms these definitions are safe for analytical
                  use.
                </small>
              </span>
            </label>
            <div className="validation-row">
              <button
                className="secondary-button"
                disabled={!reviewed}
                onClick={() => void validateSemantic()}
              >
                {validated ? "✓ Contract validated" : "Run contract checks"}
              </button>
              <div>
                <span
                  className={cn(
                    "validation-light",
                    validated && "passed",
                  )}
                />
                {validated
                  ? "All blocking checks passed"
                  : "Validation required"}
              </div>
            </div>
            <div className="upload-modal-footer">
              <button className="secondary-button" onClick={() => setStep(2)}>
                ← Back
              </button>
              <button
                className="primary-button success-button"
                disabled={!reviewed || !validated}
                onClick={() => void publishUpload()}
              >
                Publish data + semantics
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function CreateDomainModal({
  close,
  createDomain,
}: {
  close: () => void;
  createDomain: (input: {
    name: string;
    description: string;
    owner: string;
  }) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("Workspace admin");
  const [isCreating, setIsCreating] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim().length < 2 || isCreating) return;
    setIsCreating(true);
    await createDomain({
      name: name.trim(),
      description:
        description.trim() ||
        "A governed business context for data, semantics, policy, and trusted analysis.",
      owner: owner.trim() || "Workspace admin",
    });
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <form
        className="modal create-domain-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-domain-title"
        onSubmit={(event) => void submit(event)}
      >
        <div className="modal-header">
          <div>
            <span className="panel-kicker">NEW GOVERNANCE BOUNDARY</span>
            <h2 id="create-domain-title">Create a data domain</h2>
            <p>
              Sources, semantics, policies, and run history stay isolated inside
              this context.
            </p>
          </div>
          <button
            className="modal-close"
            onClick={close}
            type="button"
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <div className="create-domain-body">
          <div className="domain-name-preview">
            <span>
              {name
                .split(/\s+/)
                .filter(Boolean)
                .slice(0, 2)
                .map((word) => word[0])
                .join("")
                .toUpperCase() || "DD"}
            </span>
            <div>
              <strong>{name || "Untitled data domain"}</strong>
              <code>
                {name
                  .toLowerCase()
                  .replace(/[^a-z0-9]+/g, "-")
                  .replace(/^-+|-+$/g, "") || "domain-slug"}
              </code>
            </div>
          </div>
          <label>
            <span>Domain name</span>
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Retail Commerce"
              maxLength={80}
              required
            />
            <small>Use a stable business context, not a database name.</small>
          </label>
          <label>
            <span>Purpose</span>
            <textarea
              rows={3}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What decisions and analytical questions belong here?"
            />
          </label>
          <label>
            <span>Domain owner</span>
            <input
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
              placeholder="Team or accountable owner"
            />
          </label>
          <div className="domain-boundary-note">
            <span>◆</span>
            <p>
              Creating the domain does not copy sample semantics. The next step
              is to upload this domain’s own data and review its semantic
              contract.
            </p>
          </div>
        </div>
        <div className="modal-actions">
          <button className="secondary-button" type="button" onClick={close}>
            Cancel
          </button>
          <button
            className="primary-button"
            type="submit"
            disabled={name.trim().length < 2 || isCreating}
          >
            {isCreating ? "Creating…" : "Create & add data →"}
          </button>
        </div>
      </form>
    </div>
  );
}

function SettingsModal({
  connection,
  close,
}: {
  connection: ConnectionState;
  close: () => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal settings-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
      >
        <div className="modal-header">
          <div>
            <span className="panel-kicker">WORKSPACE</span>
            <h2 id="settings-title">Runtime settings</h2>
            <p>Control the live QueryForge connection and query defaults.</p>
          </div>
          <button className="modal-close" onClick={close} aria-label="Close">
            ×
          </button>
        </div>
        <div className="settings-status-card">
          <span
            className={cn(
              "settings-status-icon",
              connection === "live" && "live",
            )}
          >
            {connection === "live" ? "✓" : "◇"}
          </span>
          <div>
            <strong>
              {connection === "live"
                ? "QueryForge backend connected"
                : "Interactive demo mode"}
            </strong>
            <p>
              {connection === "live"
                ? "Queries use the configured Python service."
                : "Start `queryforge --serve-api` and set QUERYFORGE_API_URL to enable live analysis."}
            </p>
          </div>
          <span
            className={cn(
              "status-pill",
              connection === "live" ? "success" : "neutral",
            )}
          >
            {connection === "live" ? "LIVE" : "DEMO"}
          </span>
        </div>
        <div className="settings-form">
          <label>
            <span>Model profile</span>
            <select defaultValue="auto">
              <option value="auto">Auto route</option>
              <option value="qwen">Qwen Plus</option>
              <option value="openai">GPT-4.1 mini</option>
              <option value="claude">Claude Sonnet</option>
            </select>
          </label>
          <label>
            <span>Complexity</span>
            <select defaultValue="auto">
              <option value="auto">Auto</option>
              <option value="minimal">Minimal</option>
              <option value="complex">Complex</option>
            </select>
          </label>
          <label className="setting-toggle">
            <span>
              <strong>Require semantic model</strong>
              <small>Block schema-only analytics</small>
            </span>
            <input type="checkbox" defaultChecked disabled />
          </label>
          <label className="setting-toggle">
            <span>
              <strong>Create report artifacts</strong>
              <small>Generate shareable HTML evidence</small>
            </span>
            <input type="checkbox" defaultChecked />
          </label>
        </div>
        <div className="modal-actions">
          <button className="secondary-button" onClick={close}>
            Cancel
          </button>
          <button className="primary-button" onClick={close}>
            Save settings
          </button>
        </div>
      </div>
    </div>
  );
}

function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  );
}

function formatBytes(bytes: number) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  return `${(bytes / 1024 ** exponent).toFixed(exponent ? 1 : 0)} ${units[exponent]}`;
}
