// 개발 전용 화면 미리보기 진입점. 가짜 fetch·EventSource를 설치한 뒤 앱을 그대로 렌더링한다.
// 이 파일은 preview.html에서만 불러온다. 프로덕션 빌드의 입력(index.html)에는 들어가지 않는다.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../../../docs/brand/tokens.css";
import "../styles/fonts.css";
import "../styles/base.css";
import "../styles/components.css";
import { App } from "../App";
import { PRODUCT_NAME } from "../brand";
import { installPreviewBackend, PreviewBackend } from "./fakeBackend";
import { SceneNav } from "./SceneNav";
import { runSceneActions } from "./sceneActions";
import { buildSceneData, findScene } from "./scenes";

const scene = findScene(new URLSearchParams(window.location.search).get("scene"));
document.title = `${PRODUCT_NAME} 화면 미리보기`;

// 앱이 시작되기 전에 설치해야 첫 요청부터 가짜 응답을 받는다.
installPreviewBackend(new PreviewBackend(buildSceneData(scene, Date.now())));

const root = document.getElementById("root");
if (root === null) throw new Error("#root 요소가 없다");

const navHost = document.createElement("div");
document.body.append(navHost);

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
createRoot(navHost).render(<SceneNav current={scene.id} />);

runSceneActions(scene);
