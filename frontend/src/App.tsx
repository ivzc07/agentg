import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { fetchSession } from "./api/session";
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

  if (error || !data) {
    return (
      <div className="flex items-center justify-center min-h-screen text-ink-2">
        Not signed in.
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
