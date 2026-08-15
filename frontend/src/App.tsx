import { lazy, Suspense } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { MotionConfig } from "framer-motion";
import { fetchSession, SessionAuthError } from "./api/session";
import { RosterShell } from "./components/RosterShell";
import { MemberPage } from "./components/MemberPage";
import { LoginPage } from "./components/LoginPage";
import { PresetsPage } from "./components/PresetsPage";
import { PresetsShell } from "./components/PresetsShell";
import { RoutineEditor } from "./components/RoutineEditor";

const SettingsPage = lazy(() =>
  import("./components/SettingsPage").then((module) => ({
    default: module.SettingsPage,
  })),
);

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
      <BrowserRouter>
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
    window.location.pathname.startsWith("/login");

  if (isLoginRoute) {
    return <LoginRoutes />;
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-ink-2" aria-busy="true">
        Loading…
      </div>
    );
  }

  if (error instanceof SessionAuthError) {
    return (
      <div className="flex items-center justify-center min-h-screen text-ink-2">
        <div className="text-center space-y-4 max-w-sm px-gut">
          <p className="eyebrow">Dashboard</p>
          <h1 className="text-[22px] font-semibold tracking-[-0.02em]">
            Dashboard no disponible
          </h1>
          <p className="text-[14px] text-ink-2 leading-relaxed">
            No estás autenticado. Envía <b className="text-ink font-semibold">/dashboard</b> a tu bot en Telegram
            para recibir un enlace de acceso.
          </p>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen text-ink-2 gap-4 px-gut text-center">
        <p>Something went wrong loading your session.</p>
        <button
          onClick={() => window.location.reload()}
          className="px-4 py-2 rounded-sm bg-elevation-1 border border-elevation-0-stroke text-ink hover:bg-elevation-2 transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <MotionConfig reducedMotion="user">
      <BrowserRouter>
        <Routes>
          {/* The roster in table, cards, or split view.  Split keeps the
              rail and renders the member inline, not via a deep link. */}
          <Route path="/" element={<RosterShell name={data.name} gym={data.gym} />} />
          {/* A member loaded directly (not from the Split rail) — the full
              member screen, without roster chrome around it. */}
          <Route path="members/:memberId" element={<MemberPage />} />
          {/* Settings screen (issue #153): full-page, no RosterShell chrome */}
          <Route
            path="settings"
            element={
              <Suspense
                fallback={
                  <div className="flex min-h-[200px] items-center justify-center text-muted-foreground" aria-busy="true">
                    Loading…
                  </div>
                }
              >
                <SettingsPage />
              </Suspense>
            }
          />
          {/* Presets management screen (issue #152) — standalone full-page. */}
          <Route
            path="presets"
            element={
              <PresetsShell name={data.name} gym={data.gym}>
                <PresetsPage />
              </PresetsShell>
            }
          />
          {/* The Routine editor, reached from the Member page. */}
          <Route
            path="members/:memberId/routine"
            element={<RoutineEditor />}
          />
          {/* The Preset master editor — the same editor pointed at a
              Preset (#154), reached from the Presets screen. */}
          <Route
            path="presets/:presetId/routine"
            element={<RoutineEditor preset />}
          />
          {/* Unknown deep links: get the coach back to the roster. */}
          <Route path="*" element={<Navigate to="/" replace />} />
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
