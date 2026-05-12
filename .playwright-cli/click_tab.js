async (page) => {
  await page.evaluate(() => {
    document.querySelector('a[name="ai_integration"]').click();
  });
  await page.waitForTimeout(1000);
  return 'clicked';
}
