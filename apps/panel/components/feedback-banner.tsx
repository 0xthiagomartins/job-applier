import { cn } from "@/lib/utils";

type FeedbackBannerProps = {
  kind: "success" | "error";
  message: string;
};

export function FeedbackBanner({ kind, message }: FeedbackBannerProps): React.JSX.Element {
  return (
    <div
      className={cn(
        "rounded-2xl px-4 py-3 text-sm font-semibold",
        kind === "success" && "bg-emerald-100 text-emerald-900",
        kind === "error" && "bg-rose-100 text-rose-900",
      )}
    >
      {message}
    </div>
  );
}
