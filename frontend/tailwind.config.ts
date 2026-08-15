import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // --- Design tokens mapped from dashboard.css (issue #133 redesign) ---
        // Ground & surfaces (elevation scale)
        bg: "#f7f7f8",
        "elevation-0": {
          DEFAULT: "#f7f7f8",
          stroke: "#dddde2",
        },
        "elevation-1": {
          DEFAULT: "#fafafa",
          stroke: "#dddde2",
        },
        "elevation-2": {
          DEFAULT: "#fcfcfc",
          stroke: "#b8b8c0",
        },
        "elevation-3": {
          DEFAULT: "#ffffff",
          stroke: "#6f6f78",
        },
        // GoGym v5 brand and operational accents.
        magenta: {
          DEFAULT: "#ff5a1f",
          tint: "#fff0eb",
        },
        cyan: {
          DEFAULT: "#2f6bff",
          tint: "#eaf0ff",
        },
        lime: {
          DEFAULT: "#d4ff3a",
          bright: "#e4ff5c",
          deep: "#a8d100",
        },
        flame: "#ff5a1f",
        cfdi: "#1d4ed8",
        // Severity
        coral: {
          DEFAULT: "#c6282f",
          tint: "#fcebec",
        },
        amber: {
          DEFAULT: "#9a6a00",
          tint: "#fff5d6",
        },
        purple: {
          DEFAULT: "#1d4ed8",
          tint: "#eaf0ff",
        },
        // Success (positive confirmation, e.g. copy / save / regenerate)
        success: {
          DEFAULT: "#047857",
          tint: "#e6f4ef",
        },
        // GoGym v5 ink ramp aliases used by the existing dashboard.
        ink: {
          DEFAULT: "#0a0a0b",
          2: "#5c5c66",
          3: "#6f6f78",
        },
      },
      borderRadius: {
        none: "0",
        xs: "4px",
        sm: "6px",
        md: "10px",
        lg: "14px",
        xl: "20px",
        pill: "999px",
      },
      boxShadow: {
        "shadow-1": "0 1px 0 rgba(17,17,19,0.04), 0 1px 3px rgba(17,17,19,0.05)",
        "shadow-2": "0 8px 30px -12px rgba(17,17,19,0.12), 0 2px 6px rgba(17,17,19,0.05)",
        "shadow-3": "0 12px 36px -16px rgba(17,17,19,0.18)",
        "glow-accent": "0 0 10px rgba(212,255,58,0.18)",
        "glow-accent-strong": "0 0 20px rgba(212,255,58,0.28)",
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
          '"Geist"',
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
      },
      spacing: {
        gut: "24px",
      },
      transitionDuration: {
        fast: "120ms",
      },
      backgroundImage: {
        "accent-gradient":
          "linear-gradient(135deg, #0a0a0b, #26262b)",
      },
    },
  },
  plugins: [],
} satisfies Config;
