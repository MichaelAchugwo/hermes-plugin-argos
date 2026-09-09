(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const React = SDK.React;
  const h = React.createElement;
  const { Button, Card, CardContent, Badge } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const API = "/api/plugins/argos";

  function mask(value) {
    const text = String(value || "Account");
    const at = text.indexOf("@");
    return at > 0 ? text.slice(0, 1) + "***" + text.slice(at) : text;
  }
  function pct(window) {
    return window && typeof window.remaining_pct === "number" ? Math.round(window.remaining_pct) : null;
  }
  function countdown(epoch) {
    if (!epoch) return "Reset unknown";
    const seconds = Math.max(0, Math.floor(epoch - Date.now() / 1000));
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return "Resets in " + (days ? days + "d " : "") + (hours ? hours + "h " : "") + mins + "m";
  }
  function Meter(props) {
    const value = pct(props.window);
    return h("div", { className: "cs-meter" },
      h("div", { className: "cs-meter-head" }, h("span", null, props.label), h("strong", null, value === null ? "—" : value + "%")),
      h("div", { className: "cs-track" }, h("span", { style: { width: (value === null ? 0 : value) + "%" } })),
      h("small", null, countdown(props.window && props.window.reset_at))
    );
  }
  function AccountCard(props) {
    const account = props.account;
    const windows = (account.usage && account.usage.windows) || {};
    return h(Card, { className: account.active ? "cs-card cs-card-active" : "cs-card" },
      h(CardContent, { className: "cs-card-body" },
        h("header", { className: "cs-card-head" },
          h("div", null, h("h3", null, mask(account.label)), h("p", null, (account.usage && account.usage.plan) || "Plan unavailable")),
          h("div", { className: "cs-badges" }, account.active ? h(Badge, null, "Active") : null, h(Badge, { variant: account.health.state === "ok" ? "secondary" : "destructive" }, account.health.state))
        ),
        h(Meter, { label: "5-hour window", window: windows.five_hour }),
        h(Meter, { label: "Weekly window", window: windows.weekly }),
        account.usage && account.usage.banked_resets && account.usage.banked_resets.available_count
          ? h("p", { className: "cs-credit" }, account.usage.banked_resets.available_count + " reset credit(s) banked") : null,
        h("footer", null,
          h("code", null, account.fingerprint),
          h(Button, { size: "sm", disabled: account.active || props.busy, onClick: function () { props.onUse(account.id); } }, account.active ? "In use" : "Use this account")
        )
      )
    );
  }
  function Page() {
    const [data, setData] = useState(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const load = useCallback(function (force) {
      setBusy(true); setError("");
      const call = force ? SDK.fetchJSON(API + "/refresh", { method: "POST" }) : SDK.fetchJSON(API + "/status");
      return call.then(setData).catch(function (e) { setError(e.message || String(e)); }).then(function () { setBusy(false); });
    }, []);
    useEffect(function () {
      load(false);
      const timer = setInterval(function () { load(false); }, 60000);
      return function () { clearInterval(timer); };
    }, [load]);
    function useAccount(id) {
      setBusy(true);
      SDK.fetchJSON(API + "/use", { method: "POST", body: JSON.stringify({ selector: id }), headers: { "Content-Type": "application/json" } })
        .then(function () { return load(false); }).catch(function (e) { setError(e.message || String(e)); setBusy(false); });
    }
    function toggleAuto() {
      setBusy(true);
      SDK.fetchJSON(API + "/auto", { method: "POST", body: JSON.stringify({ enabled: !data.auto_rotate }), headers: { "Content-Type": "application/json" } })
        .then(function () { return load(false); }).catch(function (e) { setError(e.message || String(e)); setBusy(false); });
    }
    return h("main", { className: "cs-page" },
      h("div", { className: "cs-title" }, h("div", null, h("h1", null, "ARGOS"), h("p", null, "Autonomous Rotation & Governance of Subscriptions")),
        h("div", { className: "cs-actions" }, h(Button, { variant: "outline", disabled: busy || !data, onClick: toggleAuto }, data && data.auto_rotate ? "Auto-rotate on" : "Auto-rotate off"), h(Button, { disabled: busy, onClick: function () { load(true); } }, busy ? "Refreshing…" : "Refresh"))
      ),
      error ? h("div", { className: "cs-error" }, error) : null,
      data && data.all_unhealthy ? h("div", { className: "cs-warning" }, "All Codex subscriptions are unavailable. Hermes fallback_model remains in control.") : null,
      !data ? h("p", { className: "cs-empty" }, "Loading subscription status…") :
      data.accounts.length === 0 ? h("p", { className: "cs-empty" }, "No accounts yet. Run hermes auth add openai-codex for each subscription.") :
      h("div", { className: "cs-grid" }, data.accounts.map(function (account) { return h(AccountCard, { key: account.id, account: account, busy: busy, onUse: useAccount }); }))
    );
  }
  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("argos", Page);
  }
})();
