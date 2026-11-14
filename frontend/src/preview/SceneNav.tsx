import { SCENES, type SceneId } from "./scenes";
import "./preview.css";

/** 화면 구석의 장면 선택. 접어 두어 앱 화면을 가리지 않는다. 개발 전용이다. */
export function SceneNav({ current }: { current: SceneId }) {
  return (
    <details className="preview-nav">
      <summary className="preview-nav-summary">
        <span className="tag">개발 전용</span> 장면 선택
      </summary>
      <nav aria-label="장면 선택">
        <ul className="preview-nav-list">
          {SCENES.map((scene) => (
            <li key={scene.id}>
              <a
                href={`?scene=${scene.id}`}
                aria-current={scene.id === current ? "page" : undefined}
              >
                {scene.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>
    </details>
  );
}
