import { AuthFrame } from "@/components/admin/auth-frame";
import { ForgotPasswordForm } from "@/app/forgot-password/forgot-password-form";

export default function ForgotPasswordPage() {
  return (
    <AuthFrame title="Reset your password" description={<>Enter your email and we&apos;ll send you a reset link.</>}>
      <ForgotPasswordForm />
    </AuthFrame>
  );
}
