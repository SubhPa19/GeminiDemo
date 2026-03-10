package com.demo.aibugs

import android.app.Activity
import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.Observer
import kotlinx.coroutines.*
import kotlin.concurrent.thread



class AntiPattern(private val context: Context) {


    companion object {

        var leakedActivity: Activity? = null

        lateinit var staticContext: Context
    }

    private val liveData = MutableLiveData<String>()

    fun startDemo(activity: Activity) {


        leakedActivity = activity


        staticContext = context


        GlobalScope.launch {
            delay(2000)
            println("Running coroutine in GlobalScope")
        }


        runBlocking {
            Thread.sleep(1500)
            println("Blocking UI thread")
        }


        liveData.observeForever(object : Observer<String> {
            override fun onChanged(t: String) {
                println("Observer triggered $t")
            }

        })

        thread {
            Handler(Looper.getMainLooper()).post {
                println("Improper thread handling")
            }
        }


        val discount = calculateDiscount(1200.0)
        println("Discount calculated: $discount")
    }


    private fun calculateDiscount(price: Double): Double {

        if (price > 1000) {
            return price * 0.8
        }

        return price * 0.9
    }

}
