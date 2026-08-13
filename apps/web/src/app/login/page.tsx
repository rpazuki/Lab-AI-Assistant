import { LoginForm } from "@/components/LoginForm";

export default function LoginPage() {
  return (
    <div className="mx-auto max-w-sm py-12">
      <h1 className="mb-1 text-xl font-semibold">Sign in</h1>
      <p className="mb-6 text-sm text-neutral-600">
        Accounts are provisioned by an administrator. There is no public access and no
        self-registration.
      </p>
      <LoginForm />
    </div>
  );
}
