import { AuthFrame } from "@/components/admin/auth-frame";
import { ResetPasswordForm } from "@/app/reset-password/reset-password-form";

interface ResetPasswordPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export default async function ResetPasswordPage({ searchParams }: ResetPasswordPageProps) {
  const params = await searchParams;
  const token = firstValue(params.token) ?? null;

  return (
    <AuthFrame title="Choose a new password" description="Must be at least 12 characters.">
      <ResetPasswordForm token={token} />
    </AuthFrame>
  );
}
