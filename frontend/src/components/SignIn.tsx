import { useState } from "react";
import { api, setApiKey } from "../lib/api";

/**
 * Sign in. Deliberately not sign *up*.
 *
 * This is an internal tool for inside-sales agents, and agents do not
 * self-register for the system that writes to customer records and releases
 * outbound messages — an administrator provisions them. A signup form here
 * would not be a missing feature politely deferred; it would be the wrong
 * shape, and it would quietly undo the access control it appears to add.
 *
 * So where a "create an account" link would sit, this says who to ask instead.
 * Stating the provisioning model is the answer to that question, not an
 * apology for lacking a form.
 *
 * The screen exists mostly to make something already true *visible*. The
 * approver on a CRM write has come from the credential rather than a text field
 * for a while, but with nobody signed in the approval band reads "local
 * operator (unauthenticated)" and that design decision is invisible. Once you
 * have signed in as Priya, the band saying "signing as Priya Nair — taken from
 * your credential, it cannot be typed" means something you watched happen.
 */

export default function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** Quick-fill, development builds only.
   *
   * Real keys must never be rendered into a shipped page. Vite replaces
   * `import.meta.env.DEV` with a literal at build time, so this whole block is
   * removed from a production bundle rather than merely hidden by CSS. */
  const demo: { key: string; label: string }[] = (import.meta as any).env?.DEV
    ? [
        { key: "k_agent_priya", label: "Priya Nair · agent" },
        { key: "k_view_anita", label: "Anita Rao · viewer" },
        { key: "k_admin_ravi", label: "Ravi Menon · admin" },
      ]
    : [];

  async function submit(candidate: string) {
    const k = candidate.trim();
    if (!k) {
      setError("Paste the key your administrator issued you.");
      return;
    }
    setBusy(true);
    setError(null);
    // Store first: /api/me reads the credential through the same path every
    // other request does, so a key that works here works everywhere.
    setApiKey(k);
    try {
      const me = await api.me();
      if (!me.authenticated) {
        setApiKey("");
        setError("That key was not recognised.");
        return;
      }
      onSignedIn();
    } catch (e: any) {
      setApiKey("");
      setError(String(e.message ?? e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <section className="card w-full max-w-md px-7 py-7">
        <span className="t-label">SahAI</span>
        <h1 className="t-speech-sm mt-2">Sign in to take calls.</h1>
        <p
          className="mt-2 text-[12.5px] leading-relaxed"
          style={{ color: "var(--graphite)" }}
        >
          Your key decides what you can do, and it is the name that goes on
          anything you approve — you cannot type a different one.
        </p>

        <form
          className="mt-6"
          onSubmit={(e) => {
            e.preventDefault();
            submit(key);
          }}
        >
          <label className="t-label mb-1.5 block" htmlFor="apikey">
            API key
          </label>
          <input
            id="apikey"
            type="password"
            autoComplete="off"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="k_live_…"
            className="card t-data w-full px-3 py-2.5"
          />
          <button
            type="submit"
            disabled={busy}
            className="btn btn-primary mt-3 w-full"
          >
            {busy ? "Checking…" : "Sign in"}
          </button>
        </form>

        {error && (
          <p
            className="mt-3 rounded-md px-3 py-2 text-[12.5px]"
            style={{ background: "var(--halt-wash)", color: "var(--halt)" }}
          >
            {error}
          </p>
        )}

        {demo.length > 0 && (
          <div className="mt-6 border-t pt-4" style={{ borderColor: "var(--hairline)" }}>
            <span className="t-label">Development only</span>
            <p className="mt-1 text-[11.5px]" style={{ color: "var(--graphite)" }}>
              Seeded accounts, compiled out of a production build.
            </p>
            <div className="mt-2 flex flex-col gap-1.5">
              {demo.map((d) => (
                <button
                  key={d.key}
                  type="button"
                  onClick={() => {
                    setKey(d.key);
                    submit(d.key);
                  }}
                  className="card px-3 py-2 text-left text-[12.5px] transition-colors hover:border-[color:var(--ink)]"
                >
                  {d.label}
                </button>
              ))}
            </div>
          </div>
        )}

        <p
          className="mt-6 border-t pt-4 text-[11.5px] leading-relaxed"
          style={{ borderColor: "var(--hairline)", color: "var(--graphite)" }}
        >
          <strong style={{ color: "var(--ink)" }}>No self-registration.</strong>{" "}
          Accounts are provisioned by your administrator. Anyone who can sign in
          here can release messages to customers and write to their records, so
          access is granted deliberately rather than claimed.
        </p>
      </section>
    </main>
  );
}
