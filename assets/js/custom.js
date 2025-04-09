
// navbar item active color js
document.addEventListener("DOMContentLoaded", function() {
    const currentPath = window.location.pathname.split('/').pop() || 'index.html';
    const navItems = document.querySelectorAll('.nav-item');
    navItems.forEach(item => {
      const link = item.querySelector('a').getAttribute('href').split('/').pop();
      if (link === currentPath) {
        item.querySelector('a').classList.add('active');
      }
    });
  });