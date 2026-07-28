import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./styles/base.css";

const root = document.getElementById("root");
if (root === null) throw new Error("нет узла #root — index.html повреждён");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
