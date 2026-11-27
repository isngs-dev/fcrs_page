import { AuthFrame } from "@/components/admin/auth-frame";
import { LoginForm } from "@/app/login/login-form";

export default function LoginPage() {
  return (
    <AuthFrame title="Sign in" description="Welcome back. Sign in to manage your chatbot.">
      <LoginForm />
    </AuthFrame>
  );
}
