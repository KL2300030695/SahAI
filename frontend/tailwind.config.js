/** @type {import("tailwindcss").Config} */
// Stock config on purpose. Layout and spacing come from core Tailwind
// utilities; the brand — colour, type, the signature marks — lives in
// index.css as tokens, so there is one place to change it and no compiler
// extension to reason about.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: { extend: {} },
  plugins: [],
};
