import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { fetchSession, SessionAuthError } from "./api/session";
import { Shell } from "./components/Shell";

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

  return <Shell name={data.name} gym={data.gym} />;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  );
}
