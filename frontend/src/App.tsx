import { BrowserRouter, Route, Routes } from "react-router-dom";

import Layout from "@/components/Layout";
import { ScanProvider } from "@/state/ScanContext";
import Architecture from "@/views/Architecture";
import Chat from "@/views/Chat";
import Inventory from "@/views/Inventory";
import Overview from "@/views/Overview";
import Savings from "@/views/Savings";
import Trends from "@/views/Trends";

export default function App() {
  return (
    <ScanProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="savings" element={<Savings />} />
            <Route path="inventory" element={<Inventory />} />
            <Route path="architecture" element={<Architecture />} />
            <Route path="chat" element={<Chat />} />
            <Route path="trends" element={<Trends />} />
            <Route path="*" element={<Overview />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ScanProvider>
  );
}
