import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { App } from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={{
      token: {
        colorPrimary: "#16665d", colorSuccess: "#28785e", colorText: "#24332f",
        colorTextSecondary: "#64756f", colorBorder: "#d8e0db",
        colorBgLayout: "#f4f6f2", borderRadius: 8, fontSize: 14,
        fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
      },
      components: {
        Table: { headerBg: "#f5f7f4", headerColor: "#52655c", cellPaddingBlock: 18 },
        Button: { primaryShadow: "none" },
      },
    }}>
      <AntApp><App /></AntApp>
    </ConfigProvider>
  </StrictMode>,
);
