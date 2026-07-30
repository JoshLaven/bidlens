(function initializeCalendarDrawers() {
  const monthFormatter = new Intl.DateTimeFormat(undefined, {month: 'long', year: 'numeric'});
  const tooltipDateFormatter = new Intl.DateTimeFormat(undefined, {month: 'long', day: 'numeric'});
  const dueDateFormatter = new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric'});

  function localDate(isoDate) {
    const [year, month, day] = isoDate.split('-').map(Number);
    return new Date(year, month - 1, day);
  }

  function isoDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  document.querySelectorAll('[data-calendar-drawer-shell]').forEach((shell) => {
    const drawer = shell.querySelector('[data-calendar-drawer]');
    const toggle = shell.querySelector('[data-calendar-drawer-toggle]');
    const closeButton = shell.querySelector('[data-calendar-drawer-close]');
    const grid = shell.querySelector('[data-calendar-grid]');
    const monthLabel = shell.querySelector('[data-calendar-month]');
    const items = JSON.parse(shell.querySelector('[data-calendar-items]')?.textContent || '[]');
    const itemsByDate = new Map();
    items.forEach((item) => {
      if (!itemsByDate.has(item.deadline)) itemsByDate.set(item.deadline, []);
      itemsByDate.get(item.deadline).push(item);
    });

    const today = new Date();
    let visibleMonth = new Date(today.getFullYear(), today.getMonth(), 1);
    let selectedDate = null;
    const stateKey = `bidlensCalendarDrawer:${shell.dataset.calendarDrawerId || drawer.id}`;

    function setOpen(open, {restoreFocus = false, focusDrawer = true} = {}) {
      shell.classList.toggle('calendar-drawer-shell--open', open);
      drawer.setAttribute('aria-hidden', String(!open));
      toggle.setAttribute('aria-expanded', String(open));
      window.sessionStorage.setItem(stateKey, open ? 'open' : 'closed');
      if (open && focusDrawer) {
        closeButton.focus({preventScroll: true});
      } else if (restoreFocus) {
        toggle.focus({preventScroll: true});
      }
    }

    function highlightCards(dateKey) {
      document.querySelectorAll('[data-response-deadline]').forEach((card) => {
        card.classList.toggle('opp-card--calendar-match', card.dataset.responseDeadline === dateKey);
      });
    }

    function buildTooltip(date, dateItems, tooltipId) {
      const tooltip = document.createElement('div');
      tooltip.id = tooltipId;
      tooltip.className = 'calendar-day-tooltip';
      tooltip.setAttribute('role', 'tooltip');

      const heading = document.createElement('strong');
      heading.textContent = tooltipDateFormatter.format(date);
      tooltip.appendChild(heading);

      if (dateItems.length > 1) {
        const count = document.createElement('span');
        count.textContent = `${dateItems.length} Opportunities`;
        tooltip.appendChild(count);
      }
      dateItems.forEach((item) => {
        const link = document.createElement('a');
        link.href = item.url;
        link.textContent = item.title;
        tooltip.appendChild(link);
      });
      const due = document.createElement('small');
      due.textContent = `Due ${dueDateFormatter.format(date)}`;
      tooltip.appendChild(due);
      return tooltip;
    }

    function render() {
      grid.replaceChildren();
      monthLabel.textContent = monthFormatter.format(visibleMonth);
      const firstCell = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth(), 1 - visibleMonth.getDay());

      for (let offset = 0; offset < 42; offset += 1) {
        const date = new Date(firstCell.getFullYear(), firstCell.getMonth(), firstCell.getDate() + offset);
        const dateKey = isoDate(date);
        const dateItems = itemsByDate.get(dateKey) || [];
        const cell = document.createElement('div');
        cell.className = 'calendar-day';
        if (date.getMonth() !== visibleMonth.getMonth()) cell.classList.add('calendar-day--outside');
        if (dateKey === isoDate(today)) cell.classList.add('calendar-day--today');
        if (dateKey === selectedDate) cell.classList.add('calendar-day--selected');

        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.calendarDate = dateKey;
        button.setAttribute('role', 'gridcell');
        button.setAttribute('aria-pressed', String(dateKey === selectedDate));
        button.setAttribute('aria-label', `${tooltipDateFormatter.format(date)}, ${dateItems.length} opportunities`);
        button.textContent = String(date.getDate());
        button.addEventListener('click', () => {
          selectedDate = dateKey;
          highlightCards(dateKey);
          render();
          grid.querySelector(`[data-calendar-date="${dateKey}"]`)?.focus({preventScroll: true});
        });
        cell.appendChild(button);

        if (dateItems.length) {
          const dots = document.createElement('span');
          dots.className = 'calendar-day-dots';
          dots.setAttribute('aria-hidden', 'true');
          const visibleDots = Math.min(dateItems.length, 4);
          for (let dot = 0; dot < visibleDots; dot += 1) dots.appendChild(document.createElement('i'));
          if (dateItems.length > visibleDots) dots.append('…');
          cell.appendChild(dots);
          const tooltipId = `${drawer.id}-tooltip-${dateKey}`;
          button.setAttribute('aria-describedby', tooltipId);
          cell.appendChild(buildTooltip(date, dateItems, tooltipId));
        }
        grid.appendChild(cell);
      }
    }

    toggle.addEventListener('click', () => setOpen(!shell.classList.contains('calendar-drawer-shell--open'), {restoreFocus: true}));
    closeButton.addEventListener('click', () => setOpen(false, {restoreFocus: true}));
    shell.querySelector('[data-calendar-previous]').addEventListener('click', () => {
      visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() - 1, 1);
      render();
    });
    shell.querySelector('[data-calendar-next]').addEventListener('click', () => {
      visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + 1, 1);
      render();
    });
    shell.querySelector('[data-calendar-today]').addEventListener('click', () => {
      visibleMonth = new Date(today.getFullYear(), today.getMonth(), 1);
      selectedDate = isoDate(today);
      highlightCards(selectedDate);
      render();
    });
    shell.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && shell.classList.contains('calendar-drawer-shell--open')) {
        event.preventDefault();
        setOpen(false, {restoreFocus: true});
        return;
      }
      const day = event.target.closest('[data-calendar-date]');
      if (!day || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      const buttons = Array.from(grid.querySelectorAll('[data-calendar-date]'));
      const delta = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7}[event.key];
      buttons[buttons.indexOf(day) + delta]?.focus({preventScroll: true});
    });

    render();
    if (window.sessionStorage.getItem(stateKey) === 'open') setOpen(true, {focusDrawer: false});
  });
})();
