export function fmtTokens(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toLocaleString("de-DE", { maximumFractionDigits: 1 })} Mio.`;
  if (v >= 10_000) return `${Math.round(v / 1000).toLocaleString("de-DE")}k`;
  if (v >= 1000) return `${(v / 1000).toLocaleString("de-DE", { maximumFractionDigits: 1 })}k`;
  return v.toLocaleString("de-DE");
}

export function fmtCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return "–";
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toLocaleString("de-DE", { maximumFractionDigits: 4, minimumFractionDigits: 3 })}`;
  return `$${usd.toLocaleString("de-DE", { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
}
