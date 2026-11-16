import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ToastProvider } from "@/components/ui/Toast";
import { AdminPage } from "@/features/admin/AdminPage";
import { ConfirmProvider } from "@/features/admin/primitives";
import "@/styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");

createRoot(container).render(
  <StrictMode>
    <ToastProvider>
      {/* 控制台的删除/驳回都要二次确认，且要能说明"会清掉多少条"——原生 confirm 装不下这些 */}
      <ConfirmProvider>
        <AdminPage />
      </ConfirmProvider>
    </ToastProvider>
  </StrictMode>,
);
