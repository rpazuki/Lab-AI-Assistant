import { LoginForm } from "@/components/LoginForm";

/** Tabler's sign-in: one narrow card centred on the page background. */
export default function LoginPage() {
  return (
    <div className="mx-auto max-w-sm py-10">
      <div className="rounded-card border border-border bg-surface p-8 shadow-card">
        <h1 className="mb-1 text-center text-page-title font-semibold text-heading">Sign in</h1>
        <p className="mb-6 text-center text-sm text-muted">
          Accounts are provisioned by an administrator. There is no public access and no
          self-registration.
        </p>
        <LoginForm />
      </div>
    </div>
  );
}
