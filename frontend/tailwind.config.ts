import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        border: "hsl(26 29% 82%)",
        input: "hsl(28 38% 96%)",
        ring: "hsl(17 60% 44%)",
        background: "hsl(34 54% 95%)",
        foreground: "hsl(21 31% 20%)",
        primary: {
          DEFAULT: "hsl(17 60% 44%)",
          foreground: "hsl(32 80% 98%)",
        },
        secondary: {
          DEFAULT: "hsl(36 64% 89%)",
          foreground: "hsl(22 31% 24%)",
        },
        muted: {
          DEFAULT: "hsl(32 45% 90%)",
          foreground: "hsl(22 12% 42%)",
        },
        accent: {
          DEFAULT: "hsl(42 78% 79%)",
          foreground: "hsl(18 43% 23%)",
        },
        card: {
          DEFAULT: "hsla(38 80% 98% / 0.86)",
          foreground: "hsl(21 31% 20%)",
        },
      },
      borderRadius: {
        xl: "1rem",
        "2xl": "1.5rem",
      },
      boxShadow: {
        panel: "0 24px 60px rgba(65, 35, 16, 0.12)",
      },
      fontFamily: {
        sans: ["Avenir Next", "Segoe UI", "sans-serif"],
        display: ["Iowan Old Style", "Palatino Linotype", "serif"],
      },
    },
  },
  plugins: [],
};

export default config;
