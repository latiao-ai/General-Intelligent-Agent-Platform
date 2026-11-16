import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ToastProvider } from "@/components/ui/Toast";
import { HubPage } from "@/features/hub/HubPage";
import "@/styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");

createRoot(container).render(
  <StrictMode>
    {/* 定时任务、连接器等视图要用 toast；旧页是全局 #toast 容器，这里换成 Provider */}
    <ToastProvider>
      <HubPage />
    </ToastProvider>
  </StrictMode>,
);
