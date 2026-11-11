import { useEffect, useState } from "react";
import { ChatScreen } from "./chat/ChatScreen";
import { ConnectionsScreen } from "./connections/ConnectionsScreen";
import {
  clearConnectionResultFromUrl,
  readConnectionResult,
  type ConnectionResult,
} from "./connections/text";
import { ConnectionErrorScreen, LoadingScreen, LoginScreen } from "./session/LoginScreen";
import { useSession } from "./session/useSession";

type View = "chat" | "connections";

export function App() {
  const { state, methods, signIn, signOut, loginError } = useSession();
  // Google 연결을 마치고 돌아오면 주소에 결과가 있다. 처음 한 번만 읽고 주소에서 지운다.
  const [connectionResult] = useState<ConnectionResult | null>(() =>
    readConnectionResult(window.location.search),
  );
  const [view, setView] = useState<View>(connectionResult === null ? "chat" : "connections");
  useEffect(() => {
    clearConnectionResultFromUrl();
  }, []);

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
          onOpenConnections={() => {
            setView("connections");
          }}
        />
      );
  }
}
