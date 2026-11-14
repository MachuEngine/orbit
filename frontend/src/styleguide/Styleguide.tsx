import { useState, type ReactNode } from "react";
import tokens from "../../../docs/brand/tokens.json";
import { PRODUCT_NAME } from "../brand";
import { Accordion } from "../ui/Accordion";
import { Icon } from "../ui/Icon";
import { Envelope } from "../ui/illustrations/Envelope";
import { Halftone } from "../ui/illustrations/Halftone";
import { RetroWindow } from "../ui/RetroWindow";

// 개발 전용 스타일가이드. 토큰·글꼴·공용 컴포넌트를 한 화면에서 비교한다.
// 문구는 모두 합성 예시이며 실제 메일·이름을 쓰지 않는다.

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="sg-section">
      <h2 className="sg-h2">{title}</h2>
      {children}
    </section>
  );
}

function Swatch({ name, value }: { name: string; value: string }) {
  return (
    <li className="sg-swatch-item">
      <span className="sg-swatch" data-token={name} />
      <code className="sg-code">--color-{name}</code>
      <span className="label-mono">{value}</span>
    </li>
  );
}

const COLOR_TOKENS = Object.entries(tokens.color).map(
  ([name, leaf]) => [name, leaf.$value] as const,
);

function CobaltSample({ className, title }: { className: string; title: string }) {
  return (
    <div className={`sg-surface ${className}`}>
      <p>{title}</p>
      <p>
        <a href="#colors">코발트 링크</a>
      </p>
      <p className="sg-active">활성 항목</p>
      <button type="button" className="btn btn-primary">
        주요 버튼
      </button>
    </div>
  );
}

function Colors() {
  return (
    <Section title="색">
      <ul className="sg-grid sg-swatches">
        {COLOR_TOKENS.map(([name, value]) => (
          <Swatch key={name} name={name} value={value} />
        ))}
      </ul>
      <h3 className="sg-h3">서피스</h3>
      <div className="sg-grid">
        <div className="sg-surface sg-surface-dark">
          <p>다크 서피스</p>
          <p className="sg-muted">보조 글자는 text-muted를 쓴다.</p>
        </div>
        <div className="sg-surface paper">
          <p>종이 서피스</p>
        </div>
        <div className="sg-surface section-sheet">
          <p>시트 서피스</p>
          <p>의미 색 글자는 두지 않는다.</p>
        </div>
        <div className="sg-surface section-sand">
          <p>모래 섹션</p>
        </div>
        <div className="sg-surface section-tint">
          <p>코발트 틴트 섹션</p>
        </div>
      </div>
      <h3 className="sg-h3">코발트: 링크·활성 항목 / 주요 버튼</h3>
      <p className="sg-muted sg-measure">
        어두운 바탕은 밝은 코발트(accent), 밝은 바탕은 진한 코발트(accent-on-paper)를 글자와 링크에
        쓴다. 주요 버튼은 코발트가 아니라 ink 바탕 + paper 글자 + 각진 그림자다(어두운 바탕에서는
        paper 바탕 + ink 글자).
      </p>
      <div className="sg-grid">
        <CobaltSample className="sg-surface-dark" title="다크" />
        <CobaltSample className="paper" title="종이" />
        <CobaltSample className="section-sheet" title="시트" />
        <CobaltSample className="section-sand" title="모래" />
        <CobaltSample className="section-tint" title="코발트 틴트" />
      </div>
    </Section>
  );
}

function Typography() {
  return (
    <Section title="글꼴">
      <div className="sg-stack">
        <p className="display-en">09:30 → 11:00</p>
        <p className="display-ko">오늘 일정 3건</p>
        <p className="sg-measure">
          {PRODUCT_NAME}는 메일과 일정을 대신 읽고 정리하는 개인 비서입니다. 본문은 Pretendard 400을
          쓰고, 한 줄 길이는 한글 40자 안팎으로 제한합니다. 줄 간격은 1.7이라 긴 답변도 눈으로
          따라가기 쉽습니다.
        </p>
        <p>
          <span className="label-mono">2026-11-13 09:30 KST</span>{" "}
          <span className="label-mono">mail-index/ready</span>{" "}
          <span className="label">한글 라벨은 Pretendard</span>
        </p>
        <p className="sg-pixel">메일 색인 중 42/120 - 일정 확인 ABC abc 123</p>
      </div>
    </Section>
  );
}

function Buttons() {
  return (
    <Section title="버튼">
      <div className="sg-row">
        <button type="button" className="btn btn-secondary">
          기본
        </button>
        <button type="button" className="btn btn-primary">
          주요
        </button>
        <button type="button" className="btn btn-ink">
          검정
        </button>
        <button type="button" className="btn btn-secondary" disabled>
          비활성
        </button>
      </div>
      <p className="sg-muted sg-measure">
        포커스: Tab 키로 버튼에 이동하면 버튼 바깥 2px 간격을 두고 밝은색(밝은 바탕에서는 ink) 2px
        링이 나타난다. 높이는 최소 44px이다.
      </p>
      <h3 className="sg-h3">박스 버튼 메뉴</h3>
      <div className="sg-menu-wrap">
        <div className="sg-menu">
          <button type="button" className="btn btn-ink">
            대화 목록
          </button>
          <button type="button" className="btn btn-ink">
            알림
          </button>
          <button type="button" className="btn btn-ink">
            계정 연결
          </button>
          <button type="button" className="btn btn-primary">
            새 대화
          </button>
        </div>
      </div>
    </Section>
  );
}

function Windows() {
  return (
    <Section title="레트로 창">
      <p className="sg-muted sg-measure">
        다크 배경에서는 border 색 바깥 테두리와 도트 디더링 그림자(dither)를 기본으로 쓴다. ink
        그림자(hard)는 다크 배경에서 보이지 않아 밝은 판 위에서만 쓴다.
      </p>
      <h3 className="sg-h3">다크 배경 기본</h3>
      <div className="sg-grid sg-grid-windows">
        <RetroWindow title="메일 색인" headingLevel={4}>
          <p>메일 색인 중 42/120</p>
          <progress className="progress" max={120} value={42} aria-label="메일 색인 진행" />
        </RetroWindow>
      </div>
      <h3 className="sg-h3">밝은 판 위</h3>
      <div className="sg-surface section-sheet sg-window-on-sheet">
        <RetroWindow
          title="시트 위 창"
          headingLevel={4}
          shadow="hard"
          close={{ label: "창 닫기", onClick: () => undefined }}
        >
          <p>일정 동기화 완료</p>
        </RetroWindow>
      </div>
    </Section>
  );
}

function Cards() {
  return (
    <Section title="카드">
      <div className="sg-grid">
        <div className="crop sg-card">
          <h3 className="sg-h3">크롭마크 카드</h3>
          <p>모서리에 L자 표시를 그린다. 글자 문자는 쓰지 않는다.</p>
        </div>
        <div className="section-sheet sg-card">
          <RetroWindow title="오늘 일정" headingLevel={4} shadow="hard" compact>
            <p>09:30 주간 회의</p>
          </RetroWindow>
        </div>
        <div className="rule-left sg-card-plain">
          <h3 className="sg-h3">긴 글 세로선</h3>
          <p>
            긴 설명이나 인용은 왼쪽에 1px 선을 두고 안쪽 여백을 준다. 박스로 감싸지 않아 본문이
            답답해 보이지 않는다.
          </p>
        </div>
      </div>
    </Section>
  );
}

function Inputs() {
  const [text, setText] = useState("");
  return (
    <Section title="입력창">
      <div className="sg-composer">
        <label className="visually-hidden" htmlFor="sg-composer">
          메시지
        </label>
        <textarea
          id="sg-composer"
          className="input"
          rows={2}
          placeholder="내일 일정 알려줘"
          value={text}
          onChange={(event) => {
            setText(event.target.value);
          }}
        />
        <button type="button" className="btn btn-primary">
          보내기
        </button>
      </div>
      <p className="note tone-error">
        <Icon name="error" />
        <span>보내지 못했습니다. 네트워크를 확인하고 다시 보내 주세요.</span>
      </p>
    </Section>
  );
}

function Chat() {
  return (
    <Section title="채팅 말풍선">
      <div className="sg-chat section-sheet">
        <div className="sg-user">
          <span className="sg-tag-user tag">나</span>
          <p>내일 오전 일정 알려줘</p>
        </div>
        <div className="sg-assistant">
          <span className="tag tag-mono sg-tag-assistant">{PRODUCT_NAME}</span>
          <p>내일 오전에는 일정이 두 건 있습니다.</p>
          <ul>
            <li>09:30 주간 회의</li>
            <li>11:00 디자인 검토</li>
          </ul>
        </div>
        <div className="sg-user">
          <span className="sg-tag-user tag">나</span>
          <p>회의 전에 보낼 메일이 있는지 봐줘</p>
        </div>
        <div className="sg-assistant">
          <span className="tag tag-mono sg-tag-assistant">{PRODUCT_NAME}</span>
          <p>
            회의 자료를 요청하는 메일이 한 통 있습니다. 답장 초안을 만들어 두었으니 아래 승인 창에서
            확인해 주세요.
          </p>
        </div>
        <RetroWindow title="메일 보내기 승인" headingLevel={3} shadow="hard">
          <p>
            <span className="label">보낼 메일</span> 제목: 회의 자료 공유
          </p>
          <p className="note tone-paper-warning">
            <Icon name="warning" />
            <span>받는 사람이 처음 보는 주소입니다. 메일 본문에서 가져온 값입니다.</span>
          </p>
          <p className="note tone-paper-danger">
            <Icon name="ban" />
            <span>보낸 메일은 되돌릴 수 없습니다.</span>
          </p>
          <div className="sg-row">
            <button type="button" className="btn btn-primary">
              메일 보내기
            </button>
            <button type="button" className="btn btn-secondary">
              취소
            </button>
          </div>
        </RetroWindow>
      </div>
    </Section>
  );
}

function Tags() {
  return (
    <Section title="태그와 라벨">
      <p className="sg-row">
        <span className="tag">초안</span>
        <span className="tag tag-mono">mail.send</span>
        <span className="tag">승인 대기</span>
        <span className="label-mono">2026-11-13</span>
        <span className="label-mono">mail.read</span>
      </p>
      <h3 className="sg-h3">의미 색</h3>
      <ul className="sg-notes">
        <li className="note tone-info">
          <Icon name="info" />
          <span>정보: 메일 색인이 백그라운드에서 진행 중입니다.</span>
        </li>
        <li className="note tone-warning">
          <Icon name="warning" />
          <span>경고: 연결이 곧 만료됩니다.</span>
        </li>
        <li className="note tone-error">
          <Icon name="ban" />
          <span>위험: 메일을 삭제하면 되돌릴 수 없습니다.</span>
        </li>
        <li className="note tone-success">
          <Icon name="success" />
          <span>성공: 일정을 변경했습니다.</span>
        </li>
        <li className="note tone-error">
          <Icon name="error" />
          <span>오류: 일정을 불러오지 못했습니다.</span>
        </li>
      </ul>
    </Section>
  );
}

function Accordions() {
  return (
    <Section title="아코디언">
      <div className="sg-measure">
        <Accordion summary="보내는 메일 본문" defaultOpen>
          <p>회의 자료를 공유드립니다. 확인 후 의견 부탁드립니다.</p>
        </Accordion>
        <Accordion summary="받는 사람 3명">
          <p>합성 예시 수신자 목록이 여기에 들어간다.</p>
        </Accordion>
        <Accordion summary="실행 기록">
          <p>승인 후 서버가 실행한 결과를 시간순으로 보여 준다.</p>
        </Accordion>
      </div>
    </Section>
  );
}

function PaperTexture() {
  return (
    <Section title="종이 질감">
      <div className="sg-grid">
        <div className="sg-paper-sample paper-texture section-sheet">
          <p>시트 바탕 위의 종이 결입니다.</p>
        </div>
        <div className="sg-paper-sample paper-texture paper">
          <p>종이 카드 위의 종이 결입니다.</p>
        </div>
      </div>
    </Section>
  );
}

function Textures() {
  return (
    <Section title="텍스처와 일러스트">
      <div className="sg-grid">
        <div className="sg-texture texture-dots" />
        <div className="sg-texture texture-dither" />
        <div className="sg-illust-box">
          <Halftone className="sg-illust" />
        </div>
        <div className="sg-illust-box">
          <Envelope className="sg-illust" />
        </div>
      </div>
      <div className="sg-grid sg-grid-windows">
        <RetroWindow title="받은 메일 없음" headingLevel={3}>
          <div className="sg-empty">
            <Envelope className="sg-illust" />
            <p>아직 가져온 메일이 없습니다.</p>
            <button type="button" className="btn btn-primary">
              메일 연결하기
            </button>
          </div>
        </RetroWindow>
        <RetroWindow title="불러오는 중" headingLevel={3}>
          <div className="sg-empty">
            <p className="sg-pixel">일정을 가져오는 중</p>
            <span className="loading-dots" role="status" aria-label="불러오는 중">
              <span />
              <span />
              <span />
            </span>
          </div>
        </RetroWindow>
      </div>
    </Section>
  );
}

export function Styleguide() {
  return (
    <main className="sg">
      <header className="sg-head">
        <h1 className="sg-h1">{PRODUCT_NAME} 스타일가이드</h1>
        <p className="label">개발 전용 / 레트로 다크 · 코발트</p>
      </header>
      <Colors />
      <Typography />
      <Buttons />
      <Windows />
      <Cards />
      <Inputs />
      <Chat />
      <Tags />
      <Accordions />
      <PaperTexture />
      <Textures />
    </main>
  );
}
