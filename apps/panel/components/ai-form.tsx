"use client";

import { useEffect, useState, useTransition } from "react";

import { FeedbackBanner } from "@/components/feedback-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { fetchPanelState, saveAiSettings } from "@/lib/api";

type FeedbackState = { kind: "success" | "error"; message: string } | null;

const modelOptions = ["o3-mini", "gpt-4.1-mini", "gpt-4o-mini"];

export function AiForm(): React.JSX.Element {
  const [model, setModel] = useState("o3-mini");
  const [apiKey, setApiKey] = useState("");
  const [maskedKey, setMaskedKey] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<FeedbackState>(null);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    void fetchPanelState()
      .then((state) => {
        setModel(state.ai.model);
        setMaskedKey(state.ai.masked_api_key);
      })
      .catch((error: Error) => setFeedback({ kind: "error", message: error.message }));
  }, []);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setFeedback(null);

    const payload = new FormData();
    payload.append("model", model);
    if (apiKey.trim()) {
      payload.append("api_key", apiKey);
    }

    startTransition(() => {
      void saveAiSettings(payload)
        .then((result) => {
          setFeedback({ kind: "success", message: result.message });
          if (apiKey.trim()) {
            setMaskedKey(`Configured (${apiKey.slice(-4)})`);
            setApiKey("");
          }
        })
        .catch((error: Error) => setFeedback({ kind: "error", message: error.message }));
    });
  }

  return (
    <form className="space-y-6" onSubmit={handleSubmit}>
      <Card className="bg-white/70">
        <CardContent className="grid gap-5 pt-6">
          <div className="space-y-2">
            <Label htmlFor="api-key">OpenAI API key</Label>
            <Input
              id="api-key"
              placeholder="sk-..."
              type="password"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
            />
            <p className="text-sm text-muted-foreground">
              {maskedKey ?? "No API key stored yet."}
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="model">Model</Label>
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger id="model">
                <SelectValue placeholder="Choose a model" />
              </SelectTrigger>
              <SelectContent>
                {modelOptions.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              Keep the MVP on lower-cost model options and start with <strong>o3-mini</strong>.
            </p>
          </div>
        </CardContent>
      </Card>

      <div className="space-y-4">
        {feedback ? <FeedbackBanner kind={feedback.kind} message={feedback.message} /> : null}
        <Button disabled={isPending} type="submit">
          {isPending ? "Saving AI settings..." : "Save AI settings"}
        </Button>
      </div>
    </form>
  );
}
