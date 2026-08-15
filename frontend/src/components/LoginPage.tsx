import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

/** Validate a login token via the peek endpoint. */
async function peekToken(token: string): Promise<boolean> {
  const response = await fetch(`/api/login/${token}`);
  if (!response.ok) return false;
  const data = await response.json();
  return data.valid === true;
}

/**
 * Interstitial / bounce screen for the ``/login/:token`` route
 * (issue #153).
 *
 * Reachable **without** a session — the server serves the SPA shell
 * unauthenticated for this route.  The login token is validated via a
 * peek API (which never spends the token) so the SPA can distinguish
 * "click to sign in" from a dead link.
 *
 * Token **redemption** stays server-side: the form POSTs to the existing
 * ``/login/:token`` server route which redeems the token and sets the
 * session cookie.
 */
export function LoginPage() {
  const { token = "" } = useParams<{ token: string }>();

  const { data: valid, isLoading } = useQuery({
    queryKey: ["login-token", token],
    queryFn: () => peekToken(token),
    staleTime: 60_000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-ink-2" aria-busy="true">
        Loading…
      </div>
    );
  }

  // Dead link: show the friendly bounce page (same wording as the
  // server-rendered door page — Spanish is the no-signal default).
  if (!valid) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-bg px-gut">
        <div className="door w-full max-w-sm text-center">
          <p className="eyebrow mb-3">Dashboard</p>
          <h1 className="text-[22px] font-semibold tracking-[-0.02em]">Este enlace ya no sirve</h1>
          <p className="text-[14px] text-ink-2 mt-3 leading-relaxed">
            Los enlaces al dashboard caducan y solo se pueden usar una vez.
            Envía <b className="text-ink font-semibold">/dashboard</b> a tu bot en Telegram para recibir uno
            nuevo.
          </p>
        </div>
      </div>
    );
  }

  // Valid token: show the interstitial with a sign-in button that POSTs
  // to the server-side redemption route.
  return (
    <div className="flex items-center justify-center min-h-screen bg-bg px-gut">
      <div className="door w-full max-w-sm text-center">
        <p className="eyebrow mb-3">Dashboard</p>
        <h1 className="text-[22px] font-semibold tracking-[-0.02em]">
          Abriendo tu dashboard…
        </h1>
        <form method="post" action={`/login/${token}`} className="mt-6">
          <button
            type="submit"
            className="w-full px-6 py-3 bg-ink text-bg font-semibold text-[14px] rounded-sm hover:bg-ink/90 transition-colors duration-fast border-ink"
          >
            Entrar al dashboard
          </button>
        </form>
        <p className="text-[13px] text-ink-3 mt-4">
          Serás redirigido a tu dashboard.
        </p>
      </div>
    </div>
  );
}
