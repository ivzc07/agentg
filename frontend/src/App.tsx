import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { MotionConfig } from "framer-motion";
import { fetchSession, SessionAuthError } from "./api/session";
import { RosterShell } from "./components/RosterShell";
import { MemberPage } from "./components/MemberPage";
import { PresetsPage } from "./components/PresetsPage";
import { PresetsShell } from "./components/PresetsShell";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: false,
    },
  },
});

export function Dashboard() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["session"],
    queryFn: fetchSession,
  });

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
        Not signed in.
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
          {/* The roster in table, cards, or split view.  Split keeps the
              rail and renders the member inline, not via a deep link. */}
          <Route path="/" element={<RosterShell name={data.name} gym={data.gym} />} />
          {/* A member loaded directly (not from the Split rail) — the full
              member screen, without roster chrome around it. */}
          <Route path="members/:memberId" element={<MemberPage />} />
          {/* Presets management screen (issue #152) — standalone full-page. */}
          <Route
            path="presets"
            element={
              <PresetsShell name={data.name} gym={data.gym}>
                <PresetsPage />
              </PresetsShell>
            }
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
