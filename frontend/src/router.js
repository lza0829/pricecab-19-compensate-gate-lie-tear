import { createRouter, createWebHistory } from 'vue-router'
import Home from './pages/Home.vue'
import Tickets from './pages/Tickets.vue'
import Apply from './pages/Apply.vue'
import PriceBook from './pages/PriceBook.vue'
import Reconcile from './pages/Reconcile.vue'
import Compensation from './pages/Compensation.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Home },
    { path: '/tickets', component: Tickets },
    { path: '/apply', component: Apply },
    { path: '/compensation', component: Compensation },
    { path: '/prices', component: PriceBook },
    { path: '/reconcile', component: Reconcile },
  ],
})
