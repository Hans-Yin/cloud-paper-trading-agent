// Execute in Codex functions.exec, where tools is available. Not a Node script.
// This collector has a fixed READ-ONLY MCP tool allowlist and no credential access.
const SYSTEM_ROOT = "/absolute/path/to/cloud-paper-trading-agent";
const shellQuote = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
const ALLOWED = new Set([
  "get_accounts", "get_portfolio", "get_trade_approval_setting", "get_equity_quotes",
  "get_equity_historicals", "get_equity_tradability", "get_earnings_results"
]);
async function readTool(shortName, args) {
  if (!ALLOWED.has(shortName)) throw new Error("Collector tool is not read-only allowlisted");
  const name = "mcp__robinhood_trading__" + shortName;
  if (!ALL_TOOLS.some(t => t.name === name)) throw new Error("Robinhood tool unavailable: " + shortName);
  const response = await tools[name](args);
  if (response.isError) throw new Error("Robinhood read failed: " + shortName);
  if (response.structuredContent?.data) return response.structuredContent.data;
  const item = response.content?.find(c => c.type === "text");
  if (!item) throw new Error("Missing read response: " + shortName);
  const parsed = JSON.parse(item.text);
  if (!parsed.data) throw new Error("Missing data object: " + shortName);
  return parsed.data;
}
async function runEngine(command) {
  const result = await tools.exec_command({
    cmd: "python3 " + shellQuote(SYSTEM_ROOT + "/engine.py") + " " + command,
    workdir: SYSTEM_ROOT,
    max_output_tokens: 18000
  });
  if (result.exit_code !== 0) throw new Error("Local engine failed: " + result.output.slice(0, 400));
  return JSON.parse(result.output.trim());
}
async function saveNewJson(path, payload) {
  const source = JSON.stringify(payload, null, 2) + "\n";
  await tools.apply_patch("*** Begin Patch\n*** Add File: " + path + "\n" + source.split("\n").filter((_, i, a) => i < a.length - 1).map(line => "+" + line).join("\n") + "\n*** End Patch");
}
const req = await runEngine("requirements");
if (!req.market_open && !req.needs_bootstrap) {
  const summary = await runEngine("record-closed");
  text({status: "MARKET_CLOSED", already_applied: true, summary});
} else {
  let snapshot;
  try {
    const accountsData = await readTool("get_accounts", {});
    const accounts = (accountsData.accounts ?? []).filter(a => a && a.agentic_allowed);
    if (accounts.length !== 1) throw new Error("Exactly one accessible agentic account is required");
    const account = accounts[0];
    if (account.state !== "active" || account.deactivated || account.permanently_deactivated) throw new Error("Agentic account is not active");
    const jobs = [
      ["portfolio", () => readTool("get_portfolio", {account_number: account.account_number})],
      ["approvals", () => readTool("get_trade_approval_setting", {account_number: account.account_number})],
      ["quotes", () => readTool("get_equity_quotes", {symbols: req.all_symbols})]
    ];
    if (req.market_open) jobs.push(["intraday", () => readTool("get_equity_historicals", {
      symbols: req.all_symbols, interval: "5minute", bounds: "regular", adjustment_type: "split",
      start_time: req.intraday_start, end_time: req.asof
    })]);
    if (!req.daily_cache) {
      jobs.push(["daily", () => readTool("get_equity_historicals", {
        symbols: req.all_symbols, interval: "day", bounds: "regular", adjustment_type: "split",
        start_time: req.daily_start, end_time: req.asof
      })]);
      jobs.push(["tradability", () => readTool("get_equity_tradability", {
        account_number: account.account_number, symbols: req.symbols
      })]);
      for (const symbol of req.symbols) jobs.push(["earnings_" + symbol, () => readTool("get_earnings_results", {symbol})]);
    }
    const responses = await Promise.allSettled(jobs.map(([, fn]) => fn()));
    const values = {};
    for (let i = 0; i < responses.length; i++) {
      if (responses[i].status !== "fulfilled") throw new Error("Required market read failed: " + jobs[i][0]);
      values[jobs[i][0]] = responses[i].value;
    }
    const indexed = results => Object.fromEntries((results ?? []).filter(Boolean).map(x => [x.symbol, x.bars ?? []]));
    let dailyCache = req.daily_cache;
    if (!dailyCache) {
      const tradability = Object.fromEntries((values.tradability.results ?? []).map(x => {
        const eligibility = (x.account_type_tradabilities ?? []).find(t => t.account_type === account.brokerage_account_type);
        return [x.symbol, x.state === "active" && x.tradeable && (!eligibility || eligibility.account_type_tradability === "tradable")];
      }));
      const earnings = {};
      for (const symbol of req.symbols) {
        const future = (values["earnings_" + symbol].results ?? []).filter(x => x && x.eps?.actual == null && x.report?.date && x.report.date >= req.local_date).sort((a, b) => a.report.date.localeCompare(b.report.date));
        earnings[symbol] = {date: future[0]?.report?.date ?? null, verified: future[0]?.report?.verified ?? false, checked_at: req.asof};
      }
      dailyCache = {session_date: req.daily_session_date, checked_date: req.local_date,
        checked_at: req.asof, bars: indexed(values.daily.results), tradability, earnings};
    }
    const portfolio = values.portfolio;
    snapshot = {source: "robinhood_mcp", status: "ok", asof: req.asof,
      real_account: {connected: true, account_suffix: account.account_number.slice(-4), state: account.state,
        value: portfolio.total_value, cash: portfolio.cash, buying_power: portfolio.buying_power?.buying_power,
        approvals_on: values.approvals.setting?.human_must_approve_trades,
        approval_updated_at: values.approvals.setting?.updated_at, checked_at: req.asof},
      quotes: Object.fromEntries((values.quotes.results ?? []).filter(x => x?.quote).map(x => [x.quote.symbol, x.quote])),
      intraday: values.intraday ? indexed(values.intraday.results) : {},
      daily_cache: dailyCache, daily_cache_ref: req.daily_cache_digest,
      tradability: dailyCache.tradability, earnings: dailyCache.earnings};
  } catch (error) {
    const reason = String(error.message ?? error).replace(/\b\d{9,}\b/g, "[redacted]").slice(0, 500);
    snapshot = {source: "robinhood_mcp", status: "error", asof: req.asof, error: reason, real_account: {connected: false}};
  }
  await saveNewJson(req.snapshot_path, snapshot);
  const plan = await runEngine("prepare --snapshot " + shellQuote(req.snapshot_path));
  // The caller (Codex) decides which candidates to accept or veto, then runs apply.
  text({snapshot_path: req.snapshot_path, plan_id: plan.plan_id, cycle_id: plan.cycle_id,
    status: plan.status, asof: plan.asof, market_open: plan.market_open,
    candidates: plan.candidates, forced_exits: plan.forced_exits,
    eligible_sells: plan.eligible_sells, equity: plan.equity,
    exposure: plan.exposure, loss_review_floor: plan.loss_review_floor,
    warnings: plan.warnings, collection_error: plan.collection_error,
    blocks: plan.blocks,
    real_account: plan.real_account,
    decision_path: req.snapshot_path.replace(/\.json$/, "-decision.json")});
}
