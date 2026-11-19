import { KeyRound } from "lucide-react";
import { useState } from "react";
import { setApiKey } from "../api";

export function KeyPrompt({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState("");
  return (
    <div className="modal-backdrop">
      <form
        className="modal small"
        onSubmit={(e) => {
          e.preventDefault();
          setApiKey(key.trim());
          onDone();
        }}
      >
        <div className="modal-icon">
          <KeyRound size={22} />
        </div>
        <h2>API-Key eingeben</h2>
        <p className="muted">
          Der Harness ist mit <code>HARNESS_API_KEY</code> geschützt. Der Key wird nur in diesem Browser gespeichert.
        </p>
        <input className="input" type="password" autoFocus value={key} onChange={(e) => setKey(e.target.value)} />
        <button className="btn primary wide" type="submit" disabled={!key.trim()}>
          Verbinden
        </button>
      </form>
    </div>
  );
}
