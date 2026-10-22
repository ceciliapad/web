// navbar item active state (also marks the parent of an active dropdown item)
document.addEventListener("DOMContentLoaded", function() {
    const currentPath = window.location.pathname.split('/').pop() || 'index.html';
    document.querySelectorAll('.navbar a[href]').forEach(link => {
      const target = link.getAttribute('href').split('/').pop();
      if (target !== currentPath) return;
      link.classList.add('active');
      link.setAttribute('aria-current', 'page');
      const dropdown = link.closest('.dropdown');
      if (dropdown) {
        dropdown.querySelector('.dropdown-toggle').classList.add('active');
      }
    });
  });
