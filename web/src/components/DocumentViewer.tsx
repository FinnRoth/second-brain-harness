import { ExternalLink, FileText, X } from "lucide-react";
import { useEffect, useState } from "react";
import { blobUrl, request } from "../api";
import type { FileInfo } from "../types";
import { Markdown } from "./Markdown";

const KIND: Record<string, string> = { pdf: "PDF", docx: "Word", pptx: "PowerPoint", xlsx: "Excel", image: "Bild", text: "Text", html: "HTML" };

export function useBlobUrl(path: string | null) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!path) return;
    let alive = true;
    let made: string | null = null;
    blobUrl(path)
      .then((u) => {
        made = u;
        if (alive) setUrl(u);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [path]);
  return url;
}

function PageImage({ fileId, page, label }: { fileId: string; page: number; label: string }) {
  const url = useBlobUrl(`/api/files/${fileId}/pages/${page}`);
  return (
    <figure className="page-thumb">
      {url ? <img src={url} alt={`${label} ${page}`} /> : <div className="page-placeholder" />}
      <figcaption>
        {label} {page}
      </figcaption>
    </figure>
  );
}

export function DocumentViewer({ fileId, onClose }: { fileId: string; onClose: () => void }) {
  const [data, setData] = useState<(FileInfo & { content: string; sections: string[] }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"text" | "pages">("text");
  const image = useBlobUrl(data?.kind === "image" ? `/api/files/${fileId}/original` : null);

  useEffect(() => {
    request(`GET`, `/api/files/${fileId}/content`).then(setData).catch((e) => setError(e.message));
  }, [fileId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const openOriginal = async () => {
    const url = await blobUrl(`/api/files/${fileId}/original`);
    window.open(url, "_blank");
  };
  const label = data?.kind === "pptx" ? "Folie" : "Seite";
  const big = (data?.content.length ?? 0) > 300_000;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal doc-viewer" onClick={(e) => e.stopPropagation()}>
        <div className="card-head">
          <FileText size={18} className="muted" />
          <h3 className="ellipsis">{data?.name ?? "Dokument"}</h3>
          <span className="grow" />
          <button className="btn small" onClick={openOriginal}>
            <ExternalLink size={14} /> Original
          </button>
          <button className="icon-btn" onClick={onClose} title="Schließen">
            <X size={18} />
          </button>
        </div>
        {error && <div className="error-box">{error}</div>}
        {data && (
          <>
            <div className="doc-meta muted small">
              {KIND[data.kind] ?? data.kind}
              {data.pages ? ` · ${data.pages} ${data.kind === "pptx" ? "Folien" : data.kind === "xlsx" ? "Tabellenblätter" : "Seiten"}` : ""}
              {data.chars ? ` · ~${data.tokens.toLocaleString("de-DE")} Tokens` : ""}
              {data.visual_pages.length > 0 && ` · ${data.visual_pages.length} ${label === "Folie" ? "Folien" : "Seiten"} zusätzlich als Bild`}
            </div>
            {data.status === "error" ? (
              <div className="error-box">Konnte nicht gelesen werden: {data.error}</div>
            ) : data.kind === "image" ? (
              <div className="doc-image">{image && <img src={image} alt={data.name} />}</div>
            ) : (
              <>
                {data.visual_pages.length > 0 && (
                  <div className="tabs">
                    <button className={tab === "text" ? "active" : ""} onClick={() => setTab("text")}>
                      Gelesener Text
                    </button>
                    <button className={tab === "pages" ? "active" : ""} onClick={() => setTab("pages")}>
                      {label === "Folie" ? "Folien" : "Seiten"} als Bild
                    </button>
                  </div>
                )}
                <div className="doc-body">
                  {tab === "text" ? (
                    big ? (
                      <pre className="doc-raw">{data.content}</pre>
                    ) : (
                      <Markdown text={data.content || "_(kein Text gefunden)_"} />
                    )
                  ) : (
                    <div className="page-grid">
                      {data.visual_pages.map((p) => (
                        <PageImage key={p} fileId={fileId} page={p} label={label} />
                      ))}
                    </div>
                  )}
                </div>
                <p className="muted small">So sieht der Assistent das Dokument. Seiten mit Grafiken oder Scans bekommt er zusätzlich als Bild.</p>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
