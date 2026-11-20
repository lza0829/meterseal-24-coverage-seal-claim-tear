import { createRouter, createWebHistory } from 'vue-router'
import Home from './pages/Home.vue'
import Meters from './pages/Meters.vue'
import Estimates from './pages/Estimates.vue'
import Actuals from './pages/Actuals.vue'
import Reconcile from './pages/Reconcile.vue'
import Seal from './pages/Seal.vue'
import Settings from './pages/Settings.vue'
import Coverage from './pages/Coverage.vue'
import Missing from './pages/Missing.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Home },
    { path: '/meters', component: Meters },
    { path: '/estimates', component: Estimates },
    { path: '/actuals', component: Actuals },
    { path: '/reconcile', component: Reconcile },
    { path: '/coverage', component: Coverage },
    { path: '/coverage/missing', component: Missing },
    { path: '/seal', component: Seal },
    { path: '/settings', component: Settings },
  ],
})
