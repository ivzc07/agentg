import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // --- Design tokens mapped from dashboard.css (issue #133 redesign) ---
        // Ground & surfaces (elevation scale)
        bg: "#000",
        "elevation-0": {
          DEFAULT: "#000",
          stroke: "#2a2b2d",
        },
        "elevation-1": {
          DEFAULT: "#131313",
          stroke: "#2a2b2d",
        },
        "elevation-2": {
          DEFAULT: "#1b1c1e",
          stroke: "#3a3a3c",
        },
        "elevation-3": {
          DEFAULT: "#242528",
          stroke: "#4a4b4e",
        },
        // Accent
        magenta: {
          DEFAULT: "#f472a7",
          tint: "#1f0d17",
        },
        cyan: {
          DEFAULT: "#4dd4e0",
          tint: "#0a1c20",
        },
        // Severity
        coral: {
          DEFAULT: "#f58060",
          tint: "#2b1712",
        },
        amber: {
          DEFAULT: "#f2b84b",
          tint: "#2a2110",
        },
        purple: {
          DEFAULT: "#8b7cf6",
          tint: "#201e33",
        },
        // Ink
        ink: {
          DEFAULT: "#fff",
          2: "#9a9a9a",
          3: "#85858a",
        },
      },
      borderRadius: {
        none: "0",
        xs: "2px",
        sm: "4px",
        md: "8px",
        pill: "999px",
      },
      boxShadow: {
        "elevation-1": "0 1px 3px rgba(0,0,0,0.4)",
        "elevation-2": "0 2px 8px rgba(0,0,0,0.5)",
        "elevation-3": "0 4px 16px rgba(0,0,0,0.6)",
        "glow-accent": "0 0 10px rgba(244, 114, 167, 0.35)",
        "glow-accent-strong": "0 0 20px rgba(244, 114, 167, 0.55)",
      },
      fontFamily: {
        mono: [
          "ui-monospace",
          '"SF Mono"',
          '"Roboto Mono"',
          "Menlo",
          "Consolas",
          "monospace",
        ],
        sans: [
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          '"Helvetica Neue"',
          "sans-serif",
        ],
      },
      spacing: {
        gut: "16px",
      },
      transitionDuration: {
        fast: "120ms",
      },
      backgroundImage: {
        "accent-gradient":
          "linear-gradient(135deg, #f472a7, #4dd4e0)",
      },
    },
  },
  plugins: [],
} satisfies Config;
