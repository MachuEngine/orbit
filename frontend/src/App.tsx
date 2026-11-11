import { ChatScreen } from "./chat/ChatScreen";
import { ConnectionErrorScreen, LoadingScreen, LoginScreen } from "./session/LoginScreen";
import { useSession } from "./session/useSession";

export function App() {
  const { state, methods, signIn, signOut, loginError } = useSession();

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
      return <ChatScreen onSignOut={() => void signOut()} />;
  }
}
