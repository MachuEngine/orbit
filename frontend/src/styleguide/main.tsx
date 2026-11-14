import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../../../docs/brand/tokens.css";
import "../styles/fonts.css";
import "../styles/base.css";
import "../styles/components.css";
import "./styleguide.css";
import { PRODUCT_NAME } from "../brand";
import { Styleguide } from "./Styleguide";

document.title = `${PRODUCT_NAME} 스타일가이드`;

const root = document.getElementById("root");
if (root === null) throw new Error("#root 요소가 없다");

createRoot(root).render(
  <StrictMode>
    <Styleguide />
  </StrictMode>,
);
