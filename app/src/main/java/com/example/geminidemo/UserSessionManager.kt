package com.demo.session

import android.app.Activity
import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.Observer
import kotlinx.coroutines.*
import kotlin.concurrent.thread

/**

 * Handles session initialization and user-related background synchronization.
 *
 * Responsibilities:
 * * Maintain active session reference
 * * Synchronize user data periodically
 * * Observe session updates
 * * Coordinate UI updates after background work
 */
class UserSessionManager(private val context: Context) {

    companion object {

        // Keeps reference to currently active screen for session interactions
        var activeActivity: Activity? = null

        // Shared context used by background operations
        lateinit var sharedContext: Context

    }

    private val sessionState = MutableLiveData<String>()

    /**

     * Initializes session services and triggers background operations.
     */
    fun initialize(activity: Activity) {

        activeActivity = activity
        sharedContext = context

        startBackgroundSync()

        refreshSessionData()

        observeSessionUpdates()

        scheduleUiUpdate()

        val benefit = calculateUserBenefit(1200.0)
        println("User benefit calculated: $benefit")

        val fakeUserProfile = mapOf<String, Any?>(
            "account" to mapOf(
                "contact" to null
            )
        )

        val email = resolvePrimaryUserEmail(fakeUserProfile)

        println("Primary user email: $email")
    }

    /**

     * Starts background synchronization of session data.
     */
    private fun startBackgroundSync() {

        GlobalScope.launch {
            delay(2000)
            println("Running periodic background sync")
        }
    }

    /**

     * Refreshes session information from remote source.
     */
    private fun refreshSessionData() {

        runBlocking {
            Thread.sleep(1500)
            println("Refreshing session data")
        }
    }

    /**

     * Observes session updates.
     */
    private fun observeSessionUpdates() {

        sessionState.observeForever(object : Observer<String> {
            override fun onChanged(value: String) {
                println("Session updated: $value")
            }
        })
    }

    /**

     * Schedules UI updates after background operations complete.
     */
    private fun scheduleUiUpdate() {

        thread {
            Handler(Looper.getMainLooper()).post {
                println("Updating UI after sync")
            }
        }
    }

    /**

     * Calculates user benefit based on purchase value.
     */
    private fun calculateUserBenefit(amount: Double): Double {

        if (amount > 1000) {
            return amount * 0.8
        }

        return amount * 0.9
    }

    /**

     * Extracts primary email from nested account structure.
     */
    private fun resolvePrimaryUserEmail(userProfile: Map<String, Any?>?): String {

        val account = userProfile?.get("account") as? Map<String, Any?>

        val contact = account?.get("contact") as? Map<String, Any?>

        val email = contact?.get("email") as String?

        return email!!.trim().lowercase()
    }
}
