/**
 * Search panel (FE-3) — Steam Workshop search and "add to queue".
 *
 * Query and results are local component state: nothing else consumes them
 * (docs/ui-redesign.md §2.2 "留在组件本地"). Adding a hit goes through
 * `store/queue` so the sidebar badge, dashboard and queue panel see the change
 * immediately.
 *
 * Legacy anchors kept for `search.spec.ts`: `#search-input`, `#btn-search`,
 * `#search-results`, `.search-hit`, `.search-hit-title`, `.search-hit-author`,
 * `button[data-id]`.
 */
import { useCallback, useState } from "react";

import { request, toMessage } from "../api/client";
import { useQueueStore } from "../store/queue";
import { notify } from "../store/ui";

interface SearchHit {
  id: string;
  title: string;
  author: string;
  description: string;
  preview_url: string;
  rating: string;
  subscribers: string;
  installed: boolean;
}

type SearchStatus = "idle" | "searching" | "done" | "error";

export function SearchPanel() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [status, setStatus] = useState<SearchStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async () => {
    const text = query.trim();
    if (!text) return;

    setStatus("searching");
    setError(null);
    try {
      const data = await request<{ results?: SearchHit[] }>("get", "/api/search", {
        query: { q: text },
      });
      setResults(data.results ?? []);
      setStatus("done");
    } catch (searchError) {
      setError(toMessage(searchError));
      setResults([]);
      setStatus("error");
      notify.error(`搜索失败: ${toMessage(searchError)}`);
    }
  }, [query]);

  const addToQueue = async (id: string) => {
    try {
      await useQueueStore.getState().add([id]);
      notify.success(`已加入队列: ${id}`);
    } catch (addError) {
      notify.error(`加入失败: ${toMessage(addError)}`);
    }
  };

  return (
    <>
      <div className="card">
        <div className="card-header">🔍 搜索 Steam Workshop</div>
        <div className="form-row">
          <input
            type="text"
            id="search-input"
            data-testid="field-search-query"
            placeholder="搜索 RimWorld Mod..."
            aria-label="搜索关键词"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void submit();
            }}
          />
          <button
            type="button"
            className="btn btn-primary"
            id="btn-search"
            data-testid="action-search"
            disabled={status === "searching"}
            onClick={() => void submit()}
          >
            {status === "searching" ? "搜索中..." : "搜索"}
          </button>
        </div>
      </div>

      <div className="card" style={{ flex: 1, overflowY: "auto" }}>
        <div id="search-results" data-testid="list-search-results">
          {status === "searching" ? (
            <span className="muted">搜索中...</span>
          ) : status === "error" ? (
            <span className="search-error" data-testid="status-search-error" role="alert">
              搜索失败: {error ?? ""}
            </span>
          ) : status === "idle" ? (
            <span className="muted">输入关键词搜索</span>
          ) : results.length === 0 ? (
            <span className="muted" data-testid="status-search-empty">
              无结果
            </span>
          ) : (
            results.map((hit) => (
              <div className="search-hit" key={hit.id} data-testid={`item-search-${hit.id}`}>
                <div className="search-hit-header">
                  <span className="search-hit-title">{hit.title}</span>
                  {hit.installed ? (
                    <span style={{ color: "var(--green)", fontSize: 11, fontWeight: 600 }}>
                      ✓ 已安装
                    </span>
                  ) : null}
                  <span className="search-hit-author">by {hit.author}</span>
                  {hit.rating ? <span className="search-hit-rating">⭐ {hit.rating}</span> : null}
                  {hit.subscribers ? (
                    <span className="search-hit-subs">{hit.subscribers} 订阅</span>
                  ) : null}
                </div>
                {hit.description ? <div className="search-hit-desc">{hit.description}</div> : null}
                <div style={{ marginTop: 6 }}>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    data-id={hit.id}
                    data-testid={`action-add-queue-${hit.id}`}
                    onClick={() => void addToQueue(hit.id)}
                  >
                    + 加入队列
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}

export default SearchPanel;
