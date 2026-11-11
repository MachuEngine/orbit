import { Icon } from "../ui/Icon";
import type { StatusItem } from "./status";

/**
 * 진행 상태와 연결 상태를 알린다. 영역은 항상 그려 두고 내용만 바꿔야 화면낭독기가 변화를 읽는다.
 * 오류는 즉시 읽는 영역(`alert`)에, 나머지는 읽던 문장이 끝난 뒤 읽는 영역(`status`)에 둔다.
 */
export function StatusBar({ items }: { items: (StatusItem | null)[] }) {
  const shown = items.filter((item): item is StatusItem => item !== null);
  const urgent = shown.filter((item) => item.tone === "error");
  const calm = shown.filter((item) => item.tone !== "error");
  return (
    <div className="status-bar">
      <div role="status">
        {calm.map((item) => (
          <StatusLine key={item.id} item={item} />
        ))}
      </div>
      <div role="alert">
        {urgent.map((item) => (
          <StatusLine key={item.id} item={item} />
        ))}
      </div>
    </div>
  );
}

function StatusLine({ item }: { item: StatusItem }) {
  return (
    <p className={`status-line tone-${item.tone}`}>
      <Icon name={item.icon} />
      <span>{item.text}</span>
    </p>
  );
}
