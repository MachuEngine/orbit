import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../../docs/brand/tokens.css";
import "./styles/fonts.css";
import "./styles/base.css";
import "./styles/components.css";
import { App } from "./App";
import { PRODUCT_NAME } from "./brand";

document.title = PRODUCT_NAME;

const root = document.getElementById("root");
if (root === null) throw new Error("#root 요소가 없다");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
