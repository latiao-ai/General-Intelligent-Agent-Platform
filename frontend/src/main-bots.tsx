import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ToastProvider } from "@/components/ui/Toast";
import { BotsPage } from "@/features/bots/BotsPage";
import "@/styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");

createRoot(container).render(
  <StrictMode>
    <ToastProvider>
      <BotsPage />
    </ToastProvider>
  </StrictMode>,
);
