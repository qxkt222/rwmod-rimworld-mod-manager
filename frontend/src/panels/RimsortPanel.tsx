/**
 * RimSort panel (FE-3) — ModsConfig.xml generation, uploaded-file compare and
 * load-order analysis.
 *
 * Generation/compare/order results live in `store/rimsort` (legacy kept them in
 * the DOM, so switching tabs must not lose them). The generated XML is also
 * downloaded as `ModsConfig.xml`, exactly like the legacy panel.
 *
 * Anchors are canonical testids: `action-rimsort-generate`,
 * `status-rimsort-output`, `dropzone-rimsort`, `field-rimsort-file`,
 * `status-rimsort-compare`, `action-rimsort-order` and
 * `status-rimsort-order`. The legacy ids are gone.
 */
import { useRef, useState } from "react";

import { toMessage } from "../api/client";
import { LoadingState } from "../components/LoadingState";
import { useQueueStore } from "../store/queue";
import { useRimsortStore } from "../store/rimsort";
import { notify } from "../store/ui";

function downloadXml(xml: string): void {
  const blob = new Blob([xml], { type: "application/xml" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "ModsConfig.xml";
  anchor.click();
  URL.revokeObjectURL(url);
}

export function RimsortPanel() {
  const xml = useRimsortStore((state) => state.xml);
  const generateStatus = useRimsortStore((state) => state.generateStatus);
  const compare = useRimsortStore((state) => state.compare);
  const compareStatus = useRimsortStore((state) => state.compareStatus);
  const compareError = useRimsortStore((state) => state.error);
  const order = useRimsortStore((state) => state.order);
  const orderStatus = useRimsortStore((state) => state.orderStatus);
  const orderError = useRimsortStore((state) => state.error);

  const fileRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleGenerate = async () => {
    try {
      const generated = await useRimsortStore.getState().generate();
      if (!generated) {
        notify.error("生成失败：未返回 ModsConfig 内容");
        return;
      }
      downloadXml(generated);
      notify.success("已生成并下载 ModsConfig.xml");
    } catch (error) {
      notify.error(`生成失败: ${toMessage(error)}`);
    }
  };

  const compareUploaded = async (file: File) => {
    try {
      await useRimsortStore.getState().compareFile(file);
    } catch (error) {
      notify.error(`对比失败: ${toMessage(error)}`);
    }
  };

  const handleFileChange = (files: FileList | null) => {
    const file = files?.[0];
    if (file) void compareUploaded(file);
  };

  const handleOrderCheck = async () => {
    try {
      await useRimsortStore.getState().checkOrder();
    } catch (error) {
      notify.error(`检查失败: ${toMessage(error)}`);
    }
  };

  const addMissingToQueue = async (workshopId: string) => {
    try {
      await useQueueStore.getState().add([workshopId]);
      notify.success(`已加入队列: ${workshopId}`);
    } catch (error) {
      notify.error(`失败: ${toMessage(error)}`);
    }
  };

  const issues = order?.issues ?? [];
  const errorCount = issues.filter((issue) => issue.severity === "error").length;
  const warnCount = issues.filter((issue) => issue.severity === "warn").length;
  const severityIcon: Record<string, string> = { error: "🔴", warn: "🟡", info: "🔵" };

  const installed = compare?.installed ?? [];
  const missing = compare?.missing ?? [];
  const extra = compare?.extra ?? [];

  return (
    <>
      <div className="card">
        <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>📐 RimSort 集成</span>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-testid="action-rimsort-generate"
            disabled={generateStatus === "loading"}
            onClick={() => void handleGenerate()}
          >
            📄 生成 ModsConfig.xml
          </button>
        </div>
        <div style={{ fontSize: 12, color: "var(--gray-text)" }}>
          从当前已安装 Mod 生成可直接导入 RimWorld 的 ModsConfig.xml。
        </div>
        {xml ? (
          <pre
            data-testid="status-rimsort-output"
            style={{
              maxHeight: 240,
              overflowY: "auto",
              fontSize: 11,
              background: "var(--surface)",
              borderRadius: 4,
              padding: 8,
              marginTop: 8,
            }}
          >
            {xml}
          </pre>
        ) : null}
      </div>

      <div className="card">
        <div className="card-header">📋 对比 ModsConfig.xml</div>
        <label
          className={`file-drop${dragOver ? " drag-over" : ""}`}
          data-testid="dropzone-rimsort"
          onDragOver={(event) => {
            event.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragOver(false);
            const files = event.dataTransfer?.files;
            if (files?.length && fileRef.current) {
              fileRef.current.files = files;
              void compareUploaded(files[0]);
            }
          }}
        >
          <div>拖拽 ModsConfig.xml 到此处，或点击选择</div>
          <input
            ref={fileRef}
            type="file"
            data-testid="field-rimsort-file"
            accept=".xml"
            onChange={(event) => handleFileChange(event.target.files)}
          />
        </label>

        <div
          data-testid="status-rimsort-compare"
          style={{ fontSize: 12, color: "var(--gray-text)", marginTop: 8 }}
        >
          {compareStatus === "loading" ? (
            <LoadingState message="正在对比..." testid="status-rimsort-compare-loading" />
          ) : compareStatus === "error" ? (
            <span className="text-error">对比失败: {compareError ?? ""}</span>
          ) : compare?.error ? (
            <span className="text-error">解析失败: {compare.error}</span>
          ) : compare ? (
            <>
              <div style={{ marginBottom: 12, fontSize: 14 }}>
                <b>对比结果</b>
                <span style={{ color: "var(--gray-text)", marginLeft: 12 }}>
                  配置中有 {compare.total_in_config ?? 0} 个 Mod
                </span>
              </div>
              <div
                style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12 }}
              >
                <span style={{ color: "var(--green)" }}>✅ 已安装 {installed.length}</span>
                <span style={{ color: "var(--red)" }}>❌ 缺失 {missing.length}</span>
                <span style={{ color: "var(--yellow)" }}>⚠ 额外 {extra.length}</span>
              </div>
              {missing.length > 0 ? (
                <div style={{ marginTop: 8 }}>
                  <div style={{ fontWeight: 600, fontSize: 12, color: "var(--red)" }}>
                    缺失的 Mod ({missing.length})
                  </div>
                  {missing.map((packageId, index) => {
                    const detail = compare.missing_details?.[index];
                    const workshopId = detail?.workshop_id ?? "";
                    return (
                      <div className="mod-row" key={`${packageId}-${index}`}>
                        <div
                          className="mod-icon"
                          style={{ background: "var(--red)", color: "#fff" }}
                        >
                          ❌
                        </div>
                        <div className="mod-info">
                          <div className="mod-name">{packageId}</div>
                          {workshopId ? (
                            <div className="mod-meta">Workshop {workshopId}</div>
                          ) : null}
                        </div>
                        {workshopId ? (
                          <button
                            type="button"
                            className="btn btn-primary btn-sm"
                            data-testid={`action-missing-queue-${workshopId}`}
                            onClick={() => void addMissingToQueue(workshopId)}
                          >
                            ⬇ 加入队列
                          </button>
                        ) : (
                          <span style={{ color: "var(--gray-text)", fontSize: 11 }}>
                            无法解析 Workshop ID
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : null}
              {extra.length > 0 ? (
                <div style={{ marginTop: 8 }}>
                  <div style={{ fontWeight: 600, fontSize: 12, color: "var(--yellow)" }}>
                    额外安装的 Mod ({extra.length})
                  </div>
                  {extra.map((packageId) => (
                    <div className="mod-row" key={packageId}>
                      <div
                        className="mod-icon"
                        style={{ background: "var(--yellow)", color: "#fff" }}
                      >
                        ⚠
                      </div>
                      <div className="mod-info">
                        <div className="mod-name">{packageId}</div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      </div>

      <div className="card">
        <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>🔍 排序分析</span>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-testid="action-rimsort-order"
            disabled={orderStatus === "loading"}
            onClick={() => void handleOrderCheck()}
          >
            {orderStatus === "loading" ? "⏳ 检查中..." : "🔍 分析排序"}
          </button>
        </div>
        <div
          data-testid="status-rimsort-order"
          style={{ fontSize: 12, color: "var(--gray-text)" }}
        >
          {orderStatus === "loading" ? (
            <LoadingState message="正在分析加载顺序..." testid="status-rimsort-order-loading" />
          ) : orderStatus === "error" ? (
            <span className="text-error">检查失败: {orderError ?? ""}</span>
          ) : order ? (
            <>
              <div style={{ marginBottom: 12, fontSize: 14 }}>
                <b>📐 排序分析</b>
                <span style={{ color: "var(--gray-text)", marginLeft: 8 }}>
                  {order.total_mods ?? 0} 个 Mod
                </span>
              </div>
              <div style={{ display: "flex", gap: 12, marginBottom: 12 }}>
                <span style={{ color: errorCount ? "var(--red)" : "var(--green)" }}>
                  {errorCount ? `🔴 ${errorCount} 错误` : "✅ 无错误"}
                </span>
                <span style={{ color: warnCount ? "var(--yellow)" : "var(--green)" }}>
                  {warnCount ? `🟡 ${warnCount} 警告` : ""}
                </span>
                {!errorCount && !warnCount ? (
                  <span style={{ color: "var(--green)" }}>排序看起来不错！</span>
                ) : null}
              </div>
              {issues.length ? (
                issues.map((issue, index) => (
                  <div
                    key={`${issue.severity}-${index}`}
                    style={{
                      padding: "6px 8px",
                      margin: "4px 0",
                      fontSize: 12,
                      borderLeft: `3px solid ${
                        issue.severity === "error" ? "var(--red)" : "var(--yellow)"
                      }`,
                    }}
                  >
                    <span style={{ marginRight: 4 }}>{severityIcon[issue.severity] ?? ""}</span>
                    {issue.message}
                  </div>
                ))
              ) : (
                <span style={{ color: "var(--green)" }}>✅ 未发现排序问题</span>
              )}
              <div style={{ marginTop: 12, fontSize: 11, color: "var(--gray-text)" }}>
                💡 基于社区规则检测：Harmony 位置、Core/DLC 顺序、已知冲突、重复项。
                <br />
                更详细的排序分析请使用{" "}
                <a
                  href="https://github.com/rimpy-custom/RimPy"
                  target="_blank"
                  rel="noreferrer noopener"
                  style={{ color: "var(--blue)" }}
                >
                  RimPy Mod Manager
                </a>
                。
              </div>
            </>
          ) : (
            <span>点击「分析排序」检测常见问题</span>
          )}
        </div>
      </div>
    </>
  );
}

export default RimsortPanel;
