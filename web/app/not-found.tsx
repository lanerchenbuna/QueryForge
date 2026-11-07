import Link from "next/link";

export default function NotFound() {
  return (
    <main className="not-found-page">
      <Link className="brand-mark" href="/" aria-label="QueryForge home">QF</Link>
      <p className="caption-4">QueryForge Studio · 404</p>
      <h1 className="heading-3">This page is outside the workspace</h1>
      <p className="body-3">Return to your data domains to continue exploring your data.</p>
      <Link className="primary-button" href="/">Back to workspace</Link>
    </main>
  );
}
