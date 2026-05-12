async page => {
  await page.waitForTimeout(3000);
  const chatBtn = page.locator('.o-livechat-root').locator('button').first();
  await chatBtn.click();
  await page.waitForTimeout(2000);

  // Generate ~2000 character message
  const longMsg = "我有一個非常詳細的問題想要諮詢。" +
    "我最近購買了一台筆記型電腦，型號是ABC-12345，購買日期是2025年1月15日，購買金額為新台幣35000元。".repeat(10) +
    "請問以上情況，我是否可以申請退貨或換貨？謝謝。";

  const input = page.locator('.o-livechat-root').locator('textarea').first();
  await input.fill(longMsg);
  await input.press('Enter');

  const beforeCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(500);
    const currentCount = await page.locator('.o-livechat-root').locator('.o-mail-Message').count();
    if (currentCount > beforeCount) {
      const lastMsg = page.locator('.o-livechat-root').locator('.o-mail-Message').last();
      const text = await lastMsg.textContent();
      return JSON.stringify({
        success: true,
        msgLength: longMsg.length,
        reply: text.substring(0, 300),
        msgCount: currentCount
      });
    }
  }
  return JSON.stringify({ success: false, msgLength: longMsg.length, reply: "TIMEOUT" });
}
