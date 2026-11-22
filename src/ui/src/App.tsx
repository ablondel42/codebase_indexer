import "@xyflow/react/style.css";

import { ThemeProvider } from "@/components/theme-provider";

function App() {
  return (
    <ThemeProvider
      defaultTheme="dark"
      storageKey="vite-ui-theme"
    >
      <div>42</div>
    </ThemeProvider>
  );
}

export default App;
