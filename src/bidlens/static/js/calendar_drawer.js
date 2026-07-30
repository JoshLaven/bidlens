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
    const selectedDay = shell.querySelector('[data-calendar-selected-day]');
    const selectedHeading = shell.querySelector('[data-calendar-selected-heading]');
    const selectedItems = shell.querySelector('[data-calendar-selected-items]');
    const items = JSON.parse(shell.querySelector('[data-calendar-items]')?.textContent || '[]');
    const itemsByDate = new Map();
    items.forEach((item) => {
      if (!itemsByDate.has(item.deadline)) itemsByDate.set(item.deadline, []);
      itemsByDate.get(item.deadline).push(item);
    });

    const today = new Date();
    let visibleMonth = new Date(today.getFullYear(), today.getMonth(), 1);
    let selectedDate = null;
    let tooltipHideTimer = null;
    const stateKey = `bidlensCalendarDrawer:${shell.dataset.calendarDrawerId || drawer.id}`;
    const floatingTooltip = document.createElement('div');
    floatingTooltip.id = `${drawer.id}-active-tooltip`;
    floatingTooltip.className = 'calendar-day-tooltip';
    floatingTooltip.setAttribute('role', 'tooltip');
    floatingTooltip.hidden = true;
    document.body.appendChild(floatingTooltip);

    function setOpen(open, {restoreFocus = false, focusDrawer = true} = {}) {
      shell.classList.toggle('calendar-drawer-shell--open', open);
      drawer.setAttribute('aria-hidden', String(!open));
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-pressed', String(open));
      toggle.setAttribute('aria-label', open ? 'Close shortlist calendar' : 'Open shortlist calendar');
      toggle.setAttribute('title', open ? 'Close shortlist calendar' : 'Open shortlist calendar');
      window.sessionStorage.setItem(stateKey, open ? 'open' : 'closed');
      if (!open) {
        floatingTooltip.hidden = true;
        delete floatingTooltip.dataset.visible;
      }
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

    function populateOpportunityReference(container, date, dateItems, {includeCount = true} = {}) {
      container.replaceChildren();
      const heading = document.createElement('strong');
      heading.textContent = tooltipDateFormatter.format(date);
      container.appendChild(heading);

      if (includeCount && dateItems.length > 1) {
        const count = document.createElement('span');
        count.textContent = `${dateItems.length} Opportunities`;
        container.appendChild(count);
      }
      dateItems.forEach((item) => {
        const link = document.createElement('a');
        link.href = item.url;
        link.textContent = item.title;
        container.appendChild(link);
        if (item.agency) {
          const agency = document.createElement('small');
          agency.textContent = item.agency;
          container.appendChild(agency);
        }
      });
    }

    function positionTooltip(anchor) {
      const margin = 12;
      const gap = 8;
      const anchorRect = anchor.getBoundingClientRect();
      const tooltipRect = floatingTooltip.getBoundingClientRect();
      let left = anchorRect.left + (anchorRect.width - tooltipRect.width) / 2;
      left = Math.max(margin, Math.min(left, window.innerWidth - tooltipRect.width - margin));
      let top = anchorRect.top - tooltipRect.height - gap;
      if (top < margin) top = anchorRect.bottom + gap;
      if (top + tooltipRect.height > window.innerHeight - margin) {
        top = Math.max(margin, anchorRect.top - tooltipRect.height - gap);
      }
      floatingTooltip.style.left = `${Math.round(left)}px`;
      floatingTooltip.style.top = `${Math.round(top)}px`;
    }

    function showTooltip(anchor, date, dateItems) {
      window.clearTimeout(tooltipHideTimer);
      populateOpportunityReference(floatingTooltip, date, dateItems);
      const due = document.createElement('small');
      due.textContent = `Due ${dueDateFormatter.format(date)}`;
      floatingTooltip.appendChild(due);
      floatingTooltip.hidden = false;
      floatingTooltip.dataset.visible = 'true';
      positionTooltip(anchor);
      anchor.setAttribute('aria-describedby', floatingTooltip.id);
    }

    function scheduleTooltipHide(anchor) {
      window.clearTimeout(tooltipHideTimer);
      tooltipHideTimer = window.setTimeout(() => {
        floatingTooltip.hidden = true;
        delete floatingTooltip.dataset.visible;
        anchor?.removeAttribute('aria-describedby');
      }, 80);
    }

    function renderSelectedDay() {
      if (!selectedDate) {
        selectedDay.hidden = true;
        return;
      }
      const date = localDate(selectedDate);
      const dateItems = itemsByDate.get(selectedDate) || [];
      selectedHeading.textContent = tooltipDateFormatter.format(date);
      selectedItems.replaceChildren();
      if (!dateItems.length) {
        const empty = document.createElement('p');
        empty.className = 'calendar-selected-day-empty';
        empty.textContent = 'No shortlisted opportunities due on this date';
        selectedItems.appendChild(empty);
      } else {
        dateItems.forEach((item) => {
          const entry = document.createElement('div');
          entry.className = 'calendar-selected-day-item';
          const link = document.createElement('a');
          link.href = item.url;
          link.textContent = item.title;
          entry.appendChild(link);
          if (item.agency) {
            const agency = document.createElement('small');
            agency.textContent = item.agency;
            entry.appendChild(agency);
          }
          selectedItems.appendChild(entry);
        });
      }
      selectedDay.hidden = false;
    }

    function render() {
      floatingTooltip.hidden = true;
      delete floatingTooltip.dataset.visible;
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
        button.setAttribute('aria-selected', String(dateKey === selectedDate));
        if (dateKey === isoDate(today)) button.setAttribute('aria-current', 'date');
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
          button.addEventListener('mouseenter', () => showTooltip(button, date, dateItems));
          button.addEventListener('mouseleave', () => scheduleTooltipHide(button));
          button.addEventListener('focus', () => showTooltip(button, date, dateItems));
          button.addEventListener('blur', () => scheduleTooltipHide(button));
        }
        grid.appendChild(cell);
      }
      renderSelectedDay();
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
    floatingTooltip.addEventListener('mouseenter', () => window.clearTimeout(tooltipHideTimer));
    floatingTooltip.addEventListener('mouseleave', () => scheduleTooltipHide());

    render();
    if (window.sessionStorage.getItem(stateKey) === 'open') setOpen(true, {focusDrawer: false});
  });
})();
