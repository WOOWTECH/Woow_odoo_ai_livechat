async (page) => {
  const content = await page.evaluate(() => {
    const root = document.querySelector(".o-livechat-root");
    if (!root || !root.shadowRoot) return "no shadow";
    return root.shadowRoot.innerHTML.substring(0, 800);
  });
  return content;
}
