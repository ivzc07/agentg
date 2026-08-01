import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { MotionConfig } from "framer-motion";
import { fetchSession, SessionAuthError } from "./api/session";
import { RosterShell } from "./components/RosterShell";
import { MemberPage } from "./components/MemberPage";
import { SettingsPage } from "./components/SettingsPage";
import { LoginPage } from "./components/LoginPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: false,
    },
  },
});

/**
 * The login / interstitial route is reachable **without** a session
 * (issue #153).  Everything else requires the coach to be signed in.
 */
function LoginRoutes() {
  return (
    <MotionConfig reducedMotion="user">
      <BrowserRouter basename="/dashboard">
        <Routes>
          <Route path="login/:token" element={<LoginPage />} />
          {/* Any unmatched login path shows a fallback bounce */}
          <Route path="login" element={<LoginPage />} />
        </Routes>
      </BrowserRouter>
    </MotionConfig>
  );
}

export function Dashboard() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["session"],
    queryFn: fetchSession,
  });

  // Check if we're on a login route — those don't need a session.
  const isLoginRoute =
    typeof window !== "undefined" &&
    window.location.pathname.startsWith("/dashboard/login");

  if (isLoginRoute) {
    return <LoginRoutes />;
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-ink-2">
        Loading…
      </div>
    );
  }

  if (error instanceof SessionAuthError) {
    return (
      <div className="flex items-center justify-center min-h-screen text-ink-2">
        <div className="text-center space-y-4 max-w-sm px-gut">
          <h1 className="text-[20px] font-semibold">
            Dashboard no disponible
          </h1>
          <p className="text-[14px] text-ink-2">
            No estás autenticado. Envía <b>/dashboard</b> a tu bot en Telegram
            para recibir un enlace de acceso.
          </p>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen text-ink-2 gap-4">
        <p>Something went wrong loading your session.</p>
        <button
          onClick={() => window.location.reload()}
          className="px-4 py-2 rounded bg-elevation-1 border border-elevation-0-stroke text-ink hover:bg-elevation-2 transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <MotionConfig reducedMotion="user">
      <BrowserRouter basename="/dashboard">
        <Routes>
          <Route element={<RosterShell name={data.name} gym={data.gym} />}>
            {/* Index: the roster in table, cards, or split view
                (with an empty right pane in Split). */}
            <Route index />
            {/* Nested: a member in the outlet.  In Split view the rail
                stays mounted and the member fills the right pane; in
                Table/Cards view the member renders full-page. */}
            <Route path="members/:memberId" element={<MemberPage />} />
          </Route>
          {/* Settings screen (issue #153): full-page, no RosterShell chrome */}
          <Route path="settings" element={<SettingsPage />} />
        </Routes>
      </BrowserRouter>
    </MotionConfig>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  );
}
