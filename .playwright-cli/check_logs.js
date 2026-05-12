async (page) => {
  await page.waitForTimeout(2000);

  // Click AI Integration tab
  await page.evaluate(() => {
    var tab = document.querySelector('a[name="ai_integration"]');
    if (tab) tab.click();
  });
  await page.waitForTimeout(500);

  // Click View API Logs
  const viewLogsBtn = page.getByRole('button', { name: 'View API Logs' });
  await viewLogsBtn.click();
  await page.waitForTimeout(3000);

  // Remove any default filter
  await page.evaluate(() => {
    var removeButtons = document.querySelectorAll('.o_searchview_facet .o_facet_remove');
    removeButtons.forEach(function(btn) { btn.click(); });
  });
  await page.waitForTimeout(2000);

  // Extract table data
  const data = await page.evaluate(() => {
    var rows = document.querySelectorAll('.o_list_table tbody.o_list_ungrouped tr');
    return Array.from(rows).map(function(r) {
      var cells = r.querySelectorAll('td');
      if (cells.length < 9) return null;
      return {
        timestamp: cells[1] ? cells[1].textContent.trim() : '',
        channel: cells[2] ? cells[2].textContent.trim() : '',
        session: cells[3] ? cells[3].textContent.trim() : '',
        model: cells[4] ? cells[4].textContent.trim() : '',
        status: cells[5] ? cells[5].textContent.trim() : '',
        tokens: cells[6] ? cells[6].textContent.trim() : '',
        response_time: cells[7] ? cells[7].textContent.trim() : '',
        retry_count: cells[8] ? cells[8].textContent.trim() : ''
      };
    }).filter(Boolean);
  });

  return data;
}
