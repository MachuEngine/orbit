import { useCallback, useEffect, useState } from "react";
import { ChatScreen } from "./chat/ChatScreen";
import { ConnectionsScreen } from "./connections/ConnectionsScreen";
import {
  clearConnectionResultFromUrl,
  readConnectionResult,
  type ConnectionResult,
} from "./connections/text";
import { useNotifications } from "./notifications/useNotifications";
import { ConnectionErrorScreen, LoadingScreen, LoginScreen } from "./session/LoginScreen";
import { useSession } from "./session/useSession";

type View = "chat" | "connections";

export function App() {
  const { state, methods, signIn, signOut, recheck, loginError } = useSession();
  // Google 연결을 마치고 돌아오면 주소에 결과가 있다. 처음 한 번만 읽고 주소에서 지운다.
  const [connectionResult] = useState<ConnectionResult | null>(() =>
    readConnectionResult(window.location.search),
  );
  const [view, setView] = useState<View>(connectionResult === null ? "chat" : "connections");
  useEffect(() => {
    clearConnectionResultFromUrl();
  }, []);

  const onStreamClosed = useCallback(() => void recheck(), [recheck]);
  // 로그인해 있는 동안 도착한 알림을 읽는다(화면이 바뀌어도 이어진다). 로그인이 끝나면 세션을 다시 확인한다.
  const notifications = useNotifications({
    enabled: state.phase === "signed_in",
    onUnauthorized: onStreamClosed,
  });

  switch (state.phase) {
    case "loading":
      return <LoadingScreen />;
    case "error":
      return <ConnectionErrorScreen />;
    case "signed_out":
      return (
        <LoginScreen
          methods={methods}
          devLoginFailed={state.loginFailed === true}
          loginError={loginError}
          onDevSignIn={signIn}
        />
      );
    case "signed_in":
      return view === "connections" ? (
        <ConnectionsScreen
          result={connectionResult}
          onBack={() => {
            setView("chat");
          }}
        />
      ) : (
        <ChatScreen
          onSignOut={() => void signOut()}
          onStreamClosed={onStreamClosed}
          notifications={notifications}
          onOpenConnections={() => {
            setView("connections");
          }}
        />
      );
  }
}
