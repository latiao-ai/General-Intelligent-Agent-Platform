import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ToastProvider } from "@/components/ui/Toast";
import { PlazaPage } from "@/features/plaza/PlazaPage";
import "@/styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");

createRoot(container).render(
  <StrictMode>
    <ToastProvider>
      <PlazaPage />
    </ToastProvider>
  </StrictMode>,
);
